/**********************************************************************
 * Engine.gs — Sheets Automation Platform, universal in-sheet engine.
 *
 * ONE file, identical in every sheet. Per-sheet behaviour comes ONLY from the
 * config JSON stored in Script Properties (see engine/CONFIG.md). Never edit
 * this file per sheet.
 *
 * Generalizes engine/reference/legacy.gs. Semantics mirror the Python rule
 * evaluator (backend/app/services/rules) so a backend dry-run predicts exactly
 * what this engine does.
 *
 * Flow per trigger:
 *   load config -> status ACTIVE? -> governed tab/column? -> header-hash
 *   pre-flight (mismatch => PAUSED_DRIFT, run nothing) -> evaluate every
 *   selected rule in memory -> guards -> snapshot -> batched writes (rollback
 *   on failure) -> queue run records -> best-effort flush to the backend.
 *
 * Install (manual paste-in, M2):
 *   1. Extensions > Apps Script. Delete the old code, paste this file, Save.
 *   2. Run `engineInstall` once and authorize it.
 *   3. Reload the sheet. Use Automation > Install / replace config… and paste
 *      the approved config JSON.
 *   4. Automation > Run all rules now.
 **********************************************************************/

var ENGINE_VERSION = '0.2.0';

var ENGINE = {
  KEY_CONFIG: 'engine.config',
  KEY_CONFIG_SHA: 'engine.config.sha',
  KEY_STATUS: 'engine.status',
  KEY_STATUS_REASON: 'engine.status.reason',
  KEY_QUEUE: 'engine.queue',
  KEY_QUEUE_DROPPED: 'engine.queue.dropped',
  KEY_DIRTY_PREFIX: 'engine.dirty.',
  KEY_PENDING: 'engine.pending_tabs',
  KEY_WEBHOOK_URL: 'engine.webhook_url',
  KEY_WEBHOOK_SECRET: 'engine.webhook_secret',
  KEY_FLUSH_FAILED_AT: 'engine.flush_failed_at',
  KEY_FLUSH_FAILURES: 'engine.flush_failures',
  KEY_FLUSH_LAST_ERROR: 'engine.flush_last_error',
  CHUNK_CHARS: 2000,            // 3 bytes/char worst case stays under the 9 KB property limit
  QUEUE_MAX: 200,
  FLUSH_BATCH: 100,
  FLUSH_BACKOFF_MS: 5 * 60 * 1000,
  LOCK_WAIT_MS: 20000,
  SNAPSHOT_TAB: '_engine_snapshots',
  CONFIG_TAB: '_config',
  SNAPSHOT_KEEP_RUNS: 25,
  SNAPSHOT_CELL_CHARS: 45000,
  MAX_WARNINGS: 10,
  UNKNOWN_SAMPLE_MAX: 20,
  ACTIONS: ['sort', 'format', 'consolidate', 'move', 'copy', 'validate', 'dedupe', 'clear'],
  GUARDED: { sort: true, move: true, copy: true, dedupe: true, clear: true },
  NEUTRAL: { font: '#000000', background: 'none', strike: false },
  PRESENTATION: {
    title: null, title_font: '#FFFFFF', title_background: '#38761D',
    header_font: '#000000', header_background: '#B6D7A8', banding: 'LIGHT_GREY',
    column_widths: {}, default_column_width: 130, date_format: 'match_source'
  },
  HANDLERS: ['engineOnEdit', 'engineOnOpen', 'engineTick']
};

/* =====================================================================
 * Public entry points (triggers, menu, deployer)
 * ===================================================================== */

/** Run once by hand: (re)creates the installable triggers. Rules are not run here. */
function engineInstall() {
  var ss = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach(function (t) {
    var fn = t.getHandlerFunction();
    // Remove our own triggers and orphans left behind by replaced code (e.g. legacy.gs).
    if (ENGINE.HANDLERS.indexOf(fn) !== -1 || typeof globalThis[fn] !== 'function') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('engineOnEdit').forSpreadsheet(ss).onEdit().create();
  ScriptApp.newTrigger('engineOnOpen').forSpreadsheet(ss).onOpen().create();
  ScriptApp.newTrigger('engineTick').timeBased().everyMinutes(1).create();
  enqueue_({ type: 'engine_installed' });
  notify_('Engine ' + ENGINE_VERSION + ' installed. Install a config, then run all rules.');
  return 'installed';
}

/** Removes the engine's triggers. Nothing in the sheet is locked or changed. */
function engineUninstall() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (ENGINE.HANDLERS.indexOf(t.getHandlerFunction()) !== -1) ScriptApp.deleteTrigger(t);
  });
  return 'uninstalled';
}

/** Installable open trigger: menu only (no simple triggers anywhere). */
function engineOnOpen() {
  entry_('onOpen', function () {
    SpreadsheetApp.getUi()
      .createMenu('⚙️ Automation')
      .addItem('Run all rules now', 'engineRunAll')
      .addItem('Rebuild consolidated tabs', 'engineRebuildConsolidated')
      .addItem('Undo last run', 'engineUndoLastRun')
      .addSeparator()
      .addItem('Pause automation', 'enginePause')
      .addItem('Resume automation', 'engineResume')
      .addItem('Engine status', 'engineShowStatus')
      .addSeparator()
      .addItem('Show header hashes…', 'menuShowHeaderHashes')
      .addItem('Install / replace config…', 'menuInstallConfig')
      .addItem('Set log endpoint…', 'menuSetLogEndpoint')
      .addItem('Flush logs now', 'engineFlushLogs')
      .addToUi();
  });
}

/** Installable edit trigger (runs as the owner). */
function engineOnEdit(e) {
  entry_('onEdit', function () {
    var cfg = loadConfig_();
    if (!cfg || getStatus_() !== 'ACTIVE' || !e || !e.range) return;
    var name = e.range.getSheet().getName();
    if (!hasOwn_(cfg.schema_hashes, name) || consolidateTargets_(cfg)[name]) return;
    var cols = [];
    for (var c = e.range.getColumn(); c <= e.range.getLastColumn(); c++) cols.push(c);
    var ran = withLock_(false, function () {
      runForEdit_(name, cols, false);
      flush_(false);
    });
    if (!ran) addPendingTab_(name);  // the next tick replays it
  });
}

/** 1-minute time trigger: pending edits, debounced rules, log flush. */
function engineTick() {
  entry_('tick', function () {
    withLock_(false, function () {
      var cfg = loadConfig_();
      if (cfg && getStatus_() === 'ACTIVE') {
        var pending = takePendingTabs_();
        for (var i = 0; i < pending.length; i++) runForEdit_(pending[i], [], true);
        runDueDebounced_(cfg);
      }
      flush_(false);
    });
  });
}

/** Menu / deployer: run every rule (manual event), rebuild consolidated tabs incl. widths. */
function engineRunAll() {
  return menuRun_('manual', null);
}

/** Menu: rebuild only the consolidate rules (and their chains). */
function engineRebuildConsolidated() {
  return menuRun_('debounced', null);
}

/**
 * Install a config (manual paste-in or the M3 deployer via scripts.run).
 * `options` may carry webhook_url / webhook_secret. Returns a JSON string.
 */
function engineSetConfig(json, options) {
  var result = { ok: false, errors: [] };
  withLock_(true, function () {
    var cfg;
    try {
      cfg = typeof json === 'string' ? JSON.parse(json) : json;
    } catch (err) {
      result.errors.push({ pointer: '', code: 'invalid_json', message: String(err && err.message || err) });
      return;
    }
    result.errors = checkConfigShape_(cfg);
    if (result.errors.length) return;
    var text = JSON.stringify(cfg);
    writeChunked_(ENGINE.KEY_CONFIG, text);
    props_().setProperty(ENGINE.KEY_CONFIG_SHA, sha256Hex_(text));
    CONFIG_CACHE_ = null;
    options = options || {};
    if (options.webhook_url) props_().setProperty(ENGINE.KEY_WEBHOOK_URL, String(options.webhook_url));
    if (options.webhook_secret) props_().setProperty(ENGINE.KEY_WEBHOOK_SECRET, String(options.webhook_secret));
    clearDirtyFlags_();
    setStatus_('ACTIVE', 'config ' + cfg.config_version + ' installed');
    mirrorConfig_(cfg);
    enqueue_({ type: 'config_installed', config_version: cfg.config_version });
    result.ok = true;
  });
  return JSON.stringify(result);
}

/** Pause rules. Never protects or locks anything (invariant 8). */
function enginePause() {
  withLock_(true, function () { setStatus_('PAUSED_MANUAL', 'paused from menu'); flush_(false); });
  notify_('Automation paused. Editing is unaffected.');
  return getStatus_();
}

/** Resume only if the header pre-flight passes. */
function engineResume() {
  var status = null;
  withLock_(true, function () {
    var R = newRun_('manual');
    if (!R.cfg) { status = 'NO_CONFIG'; return; }
    var drift = preflightDrift_(R);
    if (drift.length) {
      setStatus_('PAUSED_DRIFT', 'headers changed on: ' + drift.map(function (d) { return d.tab; }).join(', '));
    } else {
      setStatus_('ACTIVE', 'resumed');
    }
    status = getStatus_();
    flush_(false);
  });
  notify_('Automation status: ' + status);
  return status;
}

function engineStatus() {
  var cfg = null;
  var configError = null;
  try { cfg = loadConfig_(); } catch (err) { configError = errText_(err); logError_('engineStatus', err); }
  return JSON.stringify({
    config_error: configError,
    engine_version: ENGINE_VERSION,
    status: getStatus_(),
    reason: props_().getProperty(ENGINE.KEY_STATUS_REASON),
    config_version: cfg ? cfg.config_version : null,
    queued_logs: readQueue_().length,
    pending_tabs: readJsonProp_(ENGINE.KEY_PENDING, []),
    last_snapshot_run: lastSnapshotRunId_()
  });
}

/**
 * Header hashes for every tab, as the pre-flight computes them. Used when hand-writing a
 * config for manual install (M2); onboarding computes these server-side later.
 */
function engineHeaderHashes(headerRow) {
  var row = Number(headerRow) || 2;
  var out = {};
  SpreadsheetApp.getActive().getSheets().forEach(function (sh) {
    if (isEngineTab_(sh.getName())) return;
    out[sh.getName()] = schemaHash_(readHeaderRow_(sh, row));
  });
  return JSON.stringify(out, null, 2);
}

function menuShowHeaderHashes() {
  var ui = SpreadsheetApp.getUi();
  var res = ui.prompt('Header hashes', 'Header row number:', ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;
  ui.alert('schema_hashes (copy the governed tabs into the config)', engineHeaderHashes(res.getResponseText()),
    ui.ButtonSet.OK);
}

function engineShowStatus() {
  var s = JSON.parse(engineStatus());
  SpreadsheetApp.getUi().alert('Automation status',
    'Engine ' + s.engine_version + '\nStatus: ' + s.status + (s.reason ? ' (' + s.reason + ')' : '') +
    '\nConfig version: ' + s.config_version + '\nQueued log records: ' + s.queued_logs,
    SpreadsheetApp.getUi().ButtonSet.OK);
}

function engineFlushLogs() {
  var ok = false;
  withLock_(true, function () { ok = flush_(true); });
  notify_(ok ? 'Logs flushed.' : 'Log flush failed or no endpoint set; records kept.');
  return ok;
}

/** Restore every tab captured by the most recent run. */
function engineUndoLastRun() {
  var id = lastSnapshotRunId_();
  if (!id) { notify_('Nothing to undo.'); return null; }
  return engineUndoRun(id);
}

function engineUndoRun(runId) {
  var restored = [];
  withLock_(true, function () {
    var snaps = readSnapshots_(runId);
    var ss = SpreadsheetApp.getActive();
    snaps.forEach(function (s) {
      var sh = ss.getSheetByName(s.tab);
      if (!sh) return;
      restoreTab_(sh, s.body);
      restored.push(s.tab);
    });
    var cfg = loadConfig_();
    if (cfg && restored.length) armDirtyForTabs_(cfg, restored);
    enqueue_({ type: 'undo', run_id: runId, tabs: restored });
    flush_(false);
  });
  notify_(restored.length ? 'Restored: ' + restored.join(', ') : 'Nothing restored for run ' + runId);
  return restored;
}

function menuInstallConfig() {
  var ui = SpreadsheetApp.getUi();
  var res = ui.prompt('Install config', 'Paste the approved config JSON:', ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;
  var out = JSON.parse(engineSetConfig(res.getResponseText()));
  ui.alert(out.ok ? 'Config installed.' :
    'Config rejected:\n' + out.errors.map(function (e) { return e.pointer + ' ' + e.message; }).join('\n'));
}

function menuSetLogEndpoint() {
  var ui = SpreadsheetApp.getUi();
  var url = ui.prompt('Log endpoint', 'Backend URL for POST /webhooks/log:', ui.ButtonSet.OK_CANCEL);
  if (url.getSelectedButton() !== ui.Button.OK) return;
  var secret = ui.prompt('Log endpoint', 'Per-sheet shared secret:', ui.ButtonSet.OK_CANCEL);
  if (secret.getSelectedButton() !== ui.Button.OK) return;
  props_().setProperty(ENGINE.KEY_WEBHOOK_URL, url.getResponseText().trim());
  props_().setProperty(ENGINE.KEY_WEBHOOK_SECRET, secret.getResponseText().trim());
  props_().deleteProperty(ENGINE.KEY_FLUSH_FAILED_AT);
  ui.alert('Log endpoint saved.');
}

/* =====================================================================
 * Orchestration
 * ===================================================================== */

function entry_(name, fn) {
  try {
    return fn();
  } catch (err) {
    logError_(name, err);
    try { flush_(false); } catch (flushErr) { console.error('flush after error failed', flushErr); }
  }
}

function withLock_(wait, fn) {
  var lock = LockService.getScriptLock();
  if (wait) {
    lock.waitLock(ENGINE.LOCK_WAIT_MS);
  } else if (!lock.tryLock(ENGINE.LOCK_WAIT_MS)) {
    return false;
  }
  try {
    fn();
    return true;
  } finally {
    lock.releaseLock();
  }
}

function menuRun_(kind, ruleIds) {
  var summary = null;
  withLock_(true, function () {
    var R = newRun_(kind);
    if (!R.cfg) { summary = 'No config installed.'; return; }
    if (getStatus_() !== 'ACTIVE') {
      summary = 'Automation is ' + getStatus_() + '; nothing run.';
      return;
    }
    R.forceWidths = true;
    if (!preflight_(R)) { summary = 'Headers changed; automation paused (PAUSED_DRIFT).'; flush_(false); return; }
    var selected = selectRoots_(R, kind, ruleIds);
    var out = executeRun_(R, selected);
    if (out.ok) clearDirtyFor_(selected);
    flush_(true);
    summary = out.ok ? 'Run complete (' + out.records.length + ' rule(s)).' : 'Run refused: ' + out.error;
  });
  notify_(summary);
  return summary;
}

function runForEdit_(tabName, cols, anyColumn) {
  var R = newRun_('on_edit');
  if (!preflight_(R)) return;
  var sel = selectForEdit_(R, tabName, cols, anyColumn);
  armDirty_(sel.dirty);
  if (sel.rules.length) executeRun_(R, sel.rules);
}

function runDueDebounced_(cfg) {
  var now = Date.now();
  var due = [];
  var stamps = {};
  cfg.rules.forEach(function (rule) {
    if (!rule.trigger || !rule.trigger.debounced) return;
    var ts = props_().getProperty(ENGINE.KEY_DIRTY_PREFIX + rule.id);
    if (!ts) return;
    if (now - Number(ts) >= rule.trigger.debounced.quiet_seconds * 1000) {
      due.push(rule.id);
      stamps[rule.id] = ts;
    }
  });
  if (!due.length) return;
  var R = newRun_('debounced');
  if (!preflight_(R)) return;
  var out = executeRun_(R, selectRoots_(R, 'debounced', due));
  if (!out.ok) return;  // flags stay set -> retried next tick
  due.forEach(function (id) {
    var key = ENGINE.KEY_DIRTY_PREFIX + id;
    if (props_().getProperty(key) === stamps[id]) props_().deleteProperty(key);  // re-armed meanwhile? keep
  });
}

function newRun_(trigger) {
  var today = new Date();
  today.setHours(0, 0, 0, 0);
  var cfg = loadConfig_();
  return {
    id: Utilities.getUuid(),
    trigger: trigger,
    ss: SpreadsheetApp.getActive(),
    cfg: cfg,
    tz: Session.getScriptTimeZone(),
    today: today,
    started: Date.now(),
    tabs: {},
    headerCache: {},
    targets: cfg ? consolidateTargets_(cfg) : {},
    unknown: {},
    forceWidths: false
  };
}

/**
 * Evaluate all selected rules in memory, check guards, snapshot, then write.
 * Nothing touches the sheet unless every rule evaluated cleanly (never half-apply).
 */
function executeRun_(R, selected) {
  var plans = [];
  var consolidations = [];
  var guarded = 0;
  var failure = null;
  var maxRows = guards_(R.cfg).max_rows_per_run;

  for (var i = 0; i < selected.length && !failure; i++) {
    var rule = selected[i].rule;
    var plan = { rule: rule, rows: 0, tabs: {}, warnings: [] };
    plans.push(plan);
    try {
      evaluateRule_(R, rule, selected[i].scope, plan, consolidations);
    } catch (err) {
      if (!err || !err.ruleAbort) throw err;
      plan.error = err.message;
      failure = rule.id + ': ' + err.message;
      break;
    }
    if (ENGINE.GUARDED[rule.action]) {
      guarded += plan.rows;
      if (guarded > maxRows) {
        plan.error = 'max_rows_per_run exceeded (' + guarded + ' > ' + maxRows + ')';
        failure = rule.id + ': ' + plan.error;
      }
    }
  }

  var records = [];
  if (failure) {
    plans.forEach(function (p) {
      records.push(runRecord_(R, p, 'ERROR', p.error || ('aborted: ' + failure), null));
    });
    records.forEach(enqueue_);
    return { ok: false, error: failure, records: records };
  }

  var changed = Object.keys(R.tabs).filter(function (n) { return R.tabs[n].changed; });
  var snapshotRef = null;
  if (changed.length) {
    try {
      snapshotRef = writeSnapshots_(R, changed);
    } catch (err) {
      failure = 'snapshot failed: ' + errText_(err);
      plans.forEach(function (p) { records.push(runRecord_(R, p, 'ERROR', failure, null)); });
      records.forEach(enqueue_);
      return { ok: false, error: failure, records: records };
    }
  }

  var committed = [];
  try {
    orderedTabs_(R, changed).forEach(function (name) {
      committed.push(name);
      commitTab_(R, R.tabs[name]);
    });
  } catch (err) {
    var rollbackNote = rollback_(R, committed);
    failure = 'write failed: ' + errText_(err) + '; ' + rollbackNote;
    plans.forEach(function (p) { records.push(runRecord_(R, p, 'ERROR', failure, snapshotRef)); });
    records.forEach(enqueue_);
    return { ok: false, error: failure, records: records };
  }

  var consolidateErrors = {};
  consolidations.forEach(function (c) {
    try {
      writeConsolidated_(R, c);
    } catch (err) {
      consolidateErrors[c.rule.id] = 'consolidate write failed: ' + errText_(err);
      props_().setProperty(ENGINE.KEY_DIRTY_PREFIX + c.rule.id, String(Date.now()));  // retry next tick
    }
  });

  plans.forEach(function (p) {
    var err = consolidateErrors[p.rule.id];
    records.push(runRecord_(R, p, err ? 'ERROR' : 'OK', err || null, snapshotRef));
  });
  records.forEach(enqueue_);
  return { ok: true, records: records };
}

function runRecord_(R, plan, status, error, snapshotRef) {
  var unknown = {};
  Object.keys(R.unknown).forEach(function (k) { unknown[k] = Object.keys(R.unknown[k]); });
  return {
    type: 'run',
    run_id: R.id,
    rule_id: plan.rule.id,
    action: plan.rule.action,
    trigger_type: R.trigger,
    config_version: R.cfg.config_version,
    rows_affected: status === 'OK' ? plan.rows : 0,
    duration_ms: Date.now() - R.started,
    status: status,
    error: error,
    snapshot_ref: snapshotRef,
    tabs: Object.keys(plan.tabs),
    warnings: plan.warnings.slice(0, ENGINE.MAX_WARNINGS),
    unknown_values: unknown
  };
}

function orderedTabs_(R, names) {
  var order = R.ss.getSheets().map(function (s) { return s.getName(); });
  return names.slice().sort(function (a, b) { return order.indexOf(a) - order.indexOf(b); });
}

function rollback_(R, names) {
  var restored = [];
  var failed = [];
  names.forEach(function (name) {
    try {
      restoreTab_(R.tabs[name].sheet, R.tabs[name].before);
      restored.push(name);
    } catch (err) {
      failed.push(name + ' (' + errText_(err) + ')');
    }
  });
  return 'rolled back: [' + restored.join(', ') + ']' + (failed.length ? ' ROLLBACK FAILED: ' + failed.join(', ') : '');
}

/* =====================================================================
 * Pre-flight (invariant 4)
 * ===================================================================== */

function preflight_(R) {
  var drift = preflightDrift_(R);
  if (!drift.length) return true;
  var detail = drift.map(function (d) { return d.tab; }).join(', ');
  setStatus_('PAUSED_DRIFT', 'headers changed on: ' + detail);
  enqueue_({
    type: 'run', run_id: R.id, rule_id: null, trigger_type: R.trigger,
    config_version: R.cfg.config_version, rows_affected: 0,
    duration_ms: Date.now() - R.started, status: 'PAUSED_DRIFT',
    error: 'header drift on: ' + detail, drift: drift
  });
  return false;
}

function preflightDrift_(R) {
  var drift = [];
  Object.keys(R.cfg.schema_hashes).forEach(function (name) {
    var expected = R.cfg.schema_hashes[name];
    var sh = R.ss.getSheetByName(name);
    var actual = sh ? schemaHash_(readHeaderRow_(sh, R.cfg.header_row)) : null;
    if (actual !== expected) drift.push({ tab: name, expected: expected, actual: actual });
  });
  return drift;
}

/** sha256 of trimmed header cells (trailing blanks dropped) joined by U+001F. Mirrors preflight.py. */
function schemaHash_(cells) {
  var parts = cells.map(function (v) { return cellText_(v).trim(); });
  while (parts.length && parts[parts.length - 1] === '') parts.pop();
  return sha256Hex_(parts.join(''));
}

function readHeaderRow_(sh, headerRow) {
  var w = sh.getLastColumn();
  if (w < 1 || headerRow > sh.getMaxRows()) return [];
  return sh.getRange(headerRow, 1, 1, w).getValues()[0];
}

/* =====================================================================
 * Rule selection (mirrors runner.select_rules)
 * ===================================================================== */

function childrenMap_(cfg) {
  var map = {};
  cfg.rules.forEach(function (r) {
    if (r.trigger && r.trigger.after) (map[r.trigger.after] = map[r.trigger.after] || []).push(r);
  });
  return map;
}

function expandChains_(cfg, roots) {
  var children = childrenMap_(cfg);
  var out = [];
  function fire(rule, scope) {
    out.push({ rule: rule, scope: scope });
    (children[rule.id] || []).forEach(function (c) { fire(c, scope); });
  }
  roots.forEach(function (r) { fire(r.rule, r.scope); });
  return out;
}

function selectRoots_(R, kind, ruleIds) {
  var roots = [];
  R.cfg.rules.forEach(function (r) {
    if (r.trigger && r.trigger.after) return;
    if (ruleIds && ruleIds.indexOf(r.id) === -1) return;
    if (kind === 'debounced' && !(r.trigger && r.trigger.debounced)) return;
    if (kind === 'schedule' && !(r.trigger && r.trigger.schedule)) return;
    roots.push({ rule: r, scope: null });
  });
  return expandChains_(R.cfg, roots);
}

function selectForEdit_(R, tabName, cols, anyColumn) {
  var cfg = R.cfg;
  var result = { rules: [], dirty: [] };
  var sh = R.ss.getSheetByName(tabName);
  if (!sh || !hasOwn_(cfg.schema_hashes, tabName) || R.targets[tabName]) return result;
  var view = buildView_(cfg, headerOf_(R, tabName), false);
  var edited = {};
  view.all.forEach(function (pair) {
    if (cols.indexOf(pair[0] + 1) !== -1) edited[pair[1]] = true;
  });
  var roots = [];
  cfg.rules.forEach(function (r) {
    var trig = r.trigger || {};
    if (!trig.on_edit && !trig.debounced) return;
    var selector = r.action === 'consolidate' ? r.sources : r.tabs;
    if (resolveTabs_(R, selector, null, null).indexOf(tabName) === -1) return;
    if (trig.debounced) { result.dirty.push(r.id); return; }
    var wanted = (trig.on_edit.columns || []).map(canonKey_);
    if (!anyColumn && wanted.length && !wanted.some(function (k) { return edited[k]; })) return;
    roots.push({ rule: r, scope: r.action === 'consolidate' ? null : [tabName] });
  });
  result.rules = expandChains_(cfg, roots);
  return result;
}

function armDirty_(ruleIds) {
  var now = String(Date.now());
  ruleIds.forEach(function (id) { props_().setProperty(ENGINE.KEY_DIRTY_PREFIX + id, now); });
}

function armDirtyForTabs_(cfg, tabs) {
  var R = newRun_('debounced');
  var ids = [];
  cfg.rules.forEach(function (r) {
    if (!(r.trigger && r.trigger.debounced && r.action === 'consolidate')) return;
    var src = resolveTabs_(R, r.sources, null, null);
    if (tabs.some(function (t) { return src.indexOf(t) !== -1; })) ids.push(r.id);
  });
  armDirty_(ids);
}

function clearDirtyFor_(selected) {
  selected.forEach(function (s) {
    if (s.rule.trigger && s.rule.trigger.debounced) props_().deleteProperty(ENGINE.KEY_DIRTY_PREFIX + s.rule.id);
  });
}

function clearDirtyFlags_() {
  props_().getKeys().forEach(function (k) {
    if (k.indexOf(ENGINE.KEY_DIRTY_PREFIX) === 0) props_().deleteProperty(k);
  });
}

function addPendingTab_(name) {
  var pending = readJsonProp_(ENGINE.KEY_PENDING, []);
  if (pending.indexOf(name) === -1) pending.push(name);
  props_().setProperty(ENGINE.KEY_PENDING, JSON.stringify(pending));
}

function takePendingTabs_() {
  var pending = readJsonProp_(ENGINE.KEY_PENDING, []);
  if (pending.length) props_().deleteProperty(ENGINE.KEY_PENDING);
  return pending;
}

/* =====================================================================
 * Workbook model
 * ===================================================================== */

function consolidateTargets_(cfg) {
  var t = {};
  (cfg.rules || []).forEach(function (r) { if (r.action === 'consolidate') t[r.target_tab] = true; });
  return t;
}

function isEngineTab_(name) {
  return name === ENGINE.SNAPSHOT_TAB || name === ENGINE.CONFIG_TAB;
}

function guards_(cfg) {
  var g = cfg.guards || {};
  return {
    max_rows_per_run: g.max_rows_per_run == null ? 500 : g.max_rows_per_run,
    hold_column: g.hold_column == null ? '!hold' : g.hold_column
  };
}

function headerOf_(R, name) {
  if (R.tabs[name]) return R.tabs[name].values[R.cfg.header_row - 1] || [];
  if (!(name in R.headerCache)) {
    var sh = R.ss.getSheetByName(name);
    R.headerCache[name] = sh ? readHeaderRow_(sh, R.cfg.header_row) : [];
  }
  return R.headerCache[name];
}

function blankFmt_() { return { f: null, b: null, s: false }; }

function readFmt_(font, line, bg) {
  var f = font ? String(font).toUpperCase() : '#000000';
  var b = bg ? String(bg).toUpperCase() : null;
  if (b === '#FFFFFF') b = null;  // Sheets reports "no fill" as white
  return { f: f, b: b, s: line === 'line-through' };
}

/** Load a tab once per run; its state at first load is the run-start "before" state. */
function loadTab_(R, name) {
  if (R.tabs[name]) return R.tabs[name];
  var sh = R.ss.getSheetByName(name);
  if (!sh) return null;
  var h = sh.getLastRow();
  var w = sh.getLastColumn();
  var values = [];
  var fmt = [];
  if (h > 0 && w > 0) {
    var rng = sh.getRange(1, 1, h, w);
    values = rng.getValues();
    var fc = rng.getFontColors();
    var fl = rng.getFontLines();
    var bg = rng.getBackgrounds();
    for (var r = 0; r < h; r++) {
      var row = [];
      for (var c = 0; c < w; c++) row.push(readFmt_(fc[r][c], fl[r][c], bg[r][c]));
      fmt.push(row);
    }
  } else {
    w = 0;
  }
  var T = {
    name: name, sheet: sh, values: values, fmt: fmt, width: w,
    shadow: copyRows_(values), ops: [], fmtDirty: {}, validations: [], changed: false,
    extent: lastContentRow_(values),
    before: { height: values.length, width: w, values: copyRows_(values), fmt: copyFmt_(fmt) }
  };
  R.tabs[name] = T;
  return T;
}

function copyRows_(rows) { return rows.map(function (r) { return r.slice(); }); }
function copyFmt_(rows) {
  return rows.map(function (r) { return r.map(function (f) { return { f: f.f, b: f.b, s: f.s }; }); });
}
function blankRow_(w) { var r = []; for (var i = 0; i < w; i++) r.push(''); return r; }

function rowAt_(T, r) {
  return r >= 1 && r <= T.values.length ? T.values[r - 1] : blankRow_(T.width);
}

function ensureWidth_(T, w) {
  if (w <= T.width) return;
  [T.values, T.shadow].forEach(function (rows) {
    rows.forEach(function (row) { while (row.length < w) row.push(''); });
  });
  T.fmt.forEach(function (row) { while (row.length < w) row.push(blankFmt_()); });
  T.width = w;
}

function ensureHeight_(T, h) {
  while (T.values.length < h) {
    T.values.push(blankRow_(T.width));
    T.shadow.push(blankRow_(T.width));
    var f = [];
    for (var i = 0; i < T.width; i++) f.push(blankFmt_());
    T.fmt.push(f);
  }
}

function shiftIndexMap_(map, from, delta, removeAt) {
  var out = {};
  Object.keys(map).forEach(function (k) {
    var i = Number(k);
    if (removeAt !== null && i === removeAt) return;
    out[i >= from ? i + delta : i] = map[k];
  });
  return out;
}

/** Delete 1-based rows (model + recorded op); everything below shifts up. */
function deleteRows_(T, rows) {
  if (!rows.length) return;
  var desc = rows.slice().sort(function (a, b) { return b - a; });
  desc.forEach(function (r) {
    T.values.splice(r - 1, 1);
    T.shadow.splice(r - 1, 1);
    T.fmt.splice(r - 1, 1);
    T.fmtDirty = shiftIndexMap_(T.fmtDirty, r, -1, r - 1);
    T.validations = T.validations.filter(function (v) { return v.row !== r; })
      .map(function (v) { return v.row > r ? { row: v.row - 1, col: v.col, dv: v.dv } : v; });
  });
  T.ops.push({ del: desc });
  T.changed = true;
}

/** Insert rows so the first lands at 1-based `before` (mirrors grid.Tab.insert_rows). */
function insertRows_(T, before, rows) {
  if (!rows.length) return;
  var width = Math.max(T.width, Math.max.apply(null, rows.map(function (r) { return r.length; })));
  ensureWidth_(T, width);
  var append = before > lastContentRow_(T.shadow);
  ensureHeight_(T, before - 1);
  var padded = rows.map(function (r) { var x = r.slice(); while (x.length < width) x.push(''); return x; });
  var n = padded.length;
  var args = [before - 1, 0];
  T.values.splice.apply(T.values, args.concat(padded));
  T.shadow.splice.apply(T.shadow, args.concat(padded.map(function () { return blankRow_(width); })));
  T.fmt.splice.apply(T.fmt, args.concat(padded.map(function () { return padded[0].map(blankFmt_); })));
  T.fmtDirty = shiftIndexMap_(T.fmtDirty, before - 1, n, null);
  T.validations = T.validations.map(function (v) {
    return v.row >= before ? { row: v.row + n, col: v.col, dv: v.dv } : v;
  });
  T.ops.push({ ins: before, count: n, append: append });
  T.changed = true;
}

/* =====================================================================
 * Views, matching, conditions (mirror headers.py / conditions.py)
 * ===================================================================== */

function hasOwn_(obj, key) { return obj != null && Object.prototype.hasOwnProperty.call(obj, key); }
function isDate_(v) { return Object.prototype.toString.call(v) === '[object Date]' && !isNaN(v.getTime()); }
function isEmpty_(v) { return v === '' || v === null || v === undefined; }
function rowIsEmpty_(row) { for (var i = 0; i < row.length; i++) if (!isEmpty_(row[i])) return false; return true; }
function canonKey_(name) { return String(name).trim().toUpperCase(); }

var TZ_CACHE_ = null;
function cellText_(v) {
  if (v === null || v === undefined) return '';
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (isDate_(v)) {
    if (!TZ_CACHE_) TZ_CACHE_ = Session.getScriptTimeZone();
    var hasTime = v.getHours() || v.getMinutes() || v.getSeconds() || v.getMilliseconds();
    return Utilities.formatDate(v, TZ_CACHE_, hasTime ? "yyyy-MM-dd'T'HH:mm:ss" : 'yyyy-MM-dd');
  }
  return String(v);
}
function normText_(v) { return cellText_(v).trim().toUpperCase(); }

function lastContentRow_(rows) {
  for (var i = rows.length; i > 0; i--) if (!rowIsEmpty_(rows[i - 1])) return i;
  return 0;
}

function matchExpr_(expr, value) {
  var i = expr.indexOf(':');
  var kind = expr.slice(0, i);
  var target = expr.slice(i + 1).trim().toUpperCase();
  var hay = normText_(value);
  if (kind === 'contains') return hay.indexOf(target) !== -1;
  if (kind === 'equals') return hay === target;
  throw new Error('unknown match kind ' + kind);
}

function canonicalize_(cfg, raw) {
  var list = cfg.canonical_headers || [];
  for (var i = 0; i < list.length; i++) {
    for (var j = 0; j < list[i].match.length; j++) {
      if (matchExpr_(list[i].match[j], raw)) return list[i].canonical;
    }
  }
  return cellText_(raw).trim();
}

function buildView_(cfg, headers, rawHeaders) {
  var cols = Object.create(null);
  var display = Object.create(null);
  var all = [];
  var hold = -1;
  var holdKey = canonKey_(guards_(cfg).hold_column);
  for (var i = 0; i < headers.length; i++) {
    var raw = headers[i];
    if (isEmpty_(raw) || cellText_(raw).trim() === '') continue;
    if (normText_(raw) === holdKey) { if (hold < 0) hold = i; continue; }
    var name = rawHeaders ? cellText_(raw).trim() : canonicalize_(cfg, raw);
    var key = canonKey_(name);
    all.push([i, key]);
    if (!(key in cols)) { cols[key] = i; display[key] = name; }
  }
  return { cols: cols, display: display, all: all, hold: hold };
}

function tabView_(R, T, minEnd, rawHeaders) {
  var v = buildView_(R.cfg, T.values[R.cfg.header_row - 1] || [], rawHeaders);
  v.T = T;
  v.ds = R.cfg.data_start_row;
  var pinned = Math.min(minEnd || 0, T.values.length);
  v.de = Math.max(lastContentRow_(T.values), pinned, v.ds - 1);
  return v;
}

function viewCol_(v, name) {
  var k = canonKey_(name);
  return k in v.cols ? v.cols[k] : -1;
}

function holdIsSet_(v) {
  if (v === null || v === undefined || v === false) return false;
  if (v === true) return true;
  if (typeof v === 'number') return v !== 0;
  if (isDate_(v)) return true;
  var t = normText_(v);
  return t !== '' && t !== 'FALSE' && t !== 'NO' && t !== '0';
}

function isHeld_(v, row) { return v.hold >= 0 && holdIsSet_(row[v.hold]); }

function classify_(R, enumName, value) {
  var spec = R.cfg.enums[enumName];
  if (!isEmpty_(value)) {
    for (var i = 0; i < spec.stages.length; i++) {
      if (matchExpr_(spec.stages[i].match, value)) return { value: spec.stages[i].value, order: spec.stages[i].order };
    }
    var seen = R.unknown[enumName] = R.unknown[enumName] || {};
    var text = cellText_(value).trim();
    if (Object.keys(seen).length < ENGINE.UNKNOWN_SAMPLE_MAX) seen[text] = true;
  }
  return { value: null, order: spec.unknown_order };
}

function resolveDate_(ref, today) {
  if (ref === 'today') return new Date(today.getTime());
  var p = ref.split('-');
  return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
}

function evalCond_(cond, row, v, R, cellCol) {
  var i;
  if (hasOwn_(cond, 'all')) {
    for (i = 0; i < cond.all.length; i++) if (!evalCond_(cond.all[i], row, v, R, cellCol)) return false;
    return true;
  }
  if (hasOwn_(cond, 'any')) {
    for (i = 0; i < cond.any.length; i++) if (evalCond_(cond.any[i], row, v, R, cellCol)) return true;
    return false;
  }
  if (hasOwn_(cond, 'not')) return !evalCond_(cond.not, row, v, R, cellCol);
  if (hasOwn_(cond, 'enum')) {
    var idx = viewCol_(v, cond.column != null ? cond.column : cond.enum);
    if (idx < 0 || !hasOwn_(R.cfg.enums, cond.enum)) return false;
    var stage = classify_(R, cond.enum, row[idx]).value;
    return stage !== null && canonKey_(stage) === canonKey_(cond.is);
  }
  if (hasOwn_(cond, 'date')) {
    var di = viewCol_(v, cond.date);
    var dv = di >= 0 ? row[di] : null;
    if (!isDate_(dv)) return false;
    if (cond.before != null) return dv.getTime() < resolveDate_(cond.before, R.today).getTime();
    var after = resolveDate_(cond.after, R.today);
    after.setDate(after.getDate() + 1);
    return dv.getTime() >= after.getTime();
  }
  var col = cond.column != null ? viewCol_(v, cond.column) : (cellCol == null ? -1 : cellCol);
  if (col < 0) return false;
  var value = row[col];
  if (cond.is_blank != null) return isEmpty_(value) === cond.is_blank;
  if (cond.contains != null) return normText_(value).indexOf(String(cond.contains).trim().toUpperCase()) !== -1;
  return normText_(value) === normText_(cond.equals);
}

/* =====================================================================
 * Tab selection (mirrors rules/tabs.py)
 * ===================================================================== */

function resolveTabs_(R, selector, exclude, warnings) {
  var cfg = R.cfg;
  var out = [];
  R.ss.getSheets().forEach(function (sh) {
    var name = sh.getName();
    if (R.targets[name] || isEngineTab_(name) || (exclude && exclude[name])) return;
    if (typeof selector === 'string') {
      var column = selector.slice(selector.indexOf(':') + 1);
      if (!(canonKey_(column) in buildView_(cfg, headerOf_(R, name), false).cols)) return;
      if (!hasOwn_(cfg.schema_hashes, name)) {
        if (warnings) warnings.push("tab '" + name + "' has column '" + column + "' but is not governed; skipped");
        return;
      }
    } else if (selector.indexOf(name) === -1 || !hasOwn_(cfg.schema_hashes, name)) {
      return;
    }
    out.push(name);
  });
  if (typeof selector !== 'string' && warnings) {
    selector.forEach(function (name) {
      if (!R.ss.getSheetByName(name)) warnings.push("tab '" + name + "' not found in workbook; skipped");
    });
  }
  return out;
}

function inScope_(scope, name) { return !scope || scope.indexOf(name) !== -1; }

function abort_(message) {
  var err = new Error(message);
  err.ruleAbort = true;
  return err;
}

function addStats_(plan, tab, rowsAffected) {
  plan.tabs[tab] = true;
  plan.rows += rowsAffected;
}

/* =====================================================================
 * Rule evaluators (mirror backend/app/services/rules)
 * ===================================================================== */

function evaluateRule_(R, rule, scope, plan, consolidations) {
  switch (rule.action) {
    case 'sort': return evalSort_(R, rule, scope, plan);
    case 'format': return evalFormat_(R, rule, scope, plan);
    case 'consolidate': return evalConsolidate_(R, rule, plan, consolidations);
    case 'move':
    case 'copy': return evalTransfer_(R, rule, scope, plan);
    case 'dedupe': return evalDedupe_(R, rule, scope, plan);
    case 'clear': return evalClear_(R, rule, scope, plan);
    case 'validate': return evalValidate_(R, rule, scope, plan);
    default: throw abort_('unknown action ' + rule.action);
  }
}

// ---- sort ----------------------------------------------------------------

var ISO_DATE_RE_ = /^(\d{4})-(\d{2})-(\d{2})$/;
var NUMBER_RE_ = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

function dateValue_(v) {
  if (isDate_(v)) return v.getTime();
  if (typeof v !== 'string') return null;
  var m = ISO_DATE_RE_.exec(v.trim());
  if (!m) return null;
  var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (d.getFullYear() !== Number(m[1]) || d.getMonth() !== Number(m[2]) - 1 || d.getDate() !== Number(m[3])) return null;
  return d.getTime();
}

function sortable_(v, kind) {
  if (isEmpty_(v)) return null;
  var d;
  if (kind === 'date') { d = dateValue_(v); return d === null ? null : [0, d]; }
  if (kind === 'number') {
    if (typeof v === 'boolean') return null;
    if (typeof v === 'number') return [0, v];
    var t = cellText_(v).trim();
    return NUMBER_RE_.test(t) ? [0, Number(t)] : null;
  }
  if (kind === 'text') return [0, cellText_(v).toLowerCase()];
  if (isDate_(v)) return [0, v.getTime()];
  if (typeof v === 'boolean') return [2, v ? 1 : 0];
  if (typeof v === 'number') return [1, v];
  return [3, cellText_(v).toLowerCase()];
}

function cmp_(a, b) { return a > b ? 1 : (a < b ? -1 : 0); }
function cmpTuple_(a, b) { return cmp_(a[0], b[0]) || cmp_(a[1], b[1]); }

function keyComparator_(R, key, v) {
  var idx = viewCol_(v, key.column);
  if (idx < 0) return null;
  var sign = key.order === 'desc' ? -1 : 1;
  if (key.using_enum != null) {
    return function (a, b) {
      return sign * cmp_(classify_(R, key.using_enum, a[idx]).order, classify_(R, key.using_enum, b[idx]).order);
    };
  }
  var blankSign = key.blanks === 'first' ? -1 : 1;
  var kind = key.type || 'auto';
  return function (a, b) {
    var sa = sortable_(a[idx], kind);
    var sb = sortable_(b[idx], kind);
    if (sa === null && sb === null) return 0;
    if (sa === null) return blankSign;
    if (sb === null) return -blankSign;
    return sign * cmpTuple_(sa, sb);
  };
}

function buildComparators_(R, keys, v, warnings, tabName) {
  var out = [];
  keys.forEach(function (key) {
    var c = keyComparator_(R, key, v);
    if (c) out.push(c);
    else warnings.push("tab '" + tabName + "': sort column '" + key.column + "' missing; key skipped");
  });
  return out;
}

function sortRows_(rows, comparators) {
  var indexed = rows.map(function (row, i) { return { row: row, i: i }; });
  indexed.sort(function (a, b) {
    for (var k = 0; k < comparators.length; k++) {
      var r = comparators[k](a.row, b.row);
      if (r) return r;
    }
    return a.i - b.i;  // stable
  });
  return indexed.map(function (x) { return x.row; });
}

function evalSort_(R, rule, scope, plan) {
  resolveTabs_(R, rule.tabs, null, plan.warnings).forEach(function (name) {
    if (!inScope_(scope, name)) return;
    var T = loadTab_(R, name);
    var v = tabView_(R, T, 0, false);
    var comparators = buildComparators_(R, rule.keys, v, plan.warnings, name);
    var rows = [];
    for (var r = v.ds; r <= v.de; r++) rows.push(rowAt_(T, r));
    var slots = [];
    rows.forEach(function (row, i) { if (!isHeld_(v, row)) slots.push(i); });
    var ordered = sortRows_(slots.map(function (i) { return rows[i]; }), comparators);
    var moved = 0;
    slots.forEach(function (slot, k) {
      if (ordered[k] !== rows[slot]) moved++;
      T.values[v.ds - 1 + slot] = ordered[k];
    });
    if (moved) {
      T.changed = true;
      addStats_(plan, name, moved);
    }
  });
}

// ---- format --------------------------------------------------------------

function overlay_(fmt, style) {
  var f = style.font != null ? String(style.font).toUpperCase() : fmt.f;
  var b;
  if (style.background == null) b = fmt.b;
  else if (style.background === 'none') b = null;
  else b = String(style.background).toUpperCase();
  var s = style.strike == null ? fmt.s : !!style.strike;
  return { f: f, b: b, s: s };
}

function fmtRowEq_(a, b) {
  if (a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i].f !== b[i].f || a[i].b !== b[i].b || a[i].s !== b[i].s) return false;
  }
  return true;
}

/** Apply a format rule to the view's data rows, writing into v.T.fmt. Returns rows changed. */
function formatView_(R, rule, v, warnings) {
  var T = v.T;
  var base = rule['default'] === undefined ? ENGINE.NEUTRAL : rule['default'];
  var cellRules = rule.cell_rules || [];
  var rowRules = rule.row_rules || [];
  var cellCols = cellRules.map(function (cr) {
    var idx = viewCol_(v, cr.column);
    if (idx < 0) warnings.push("tab '" + T.name + "': format column '" + cr.column + "' missing; cell rule skipped");
    return idx;
  });
  var changed = 0;
  for (var r = v.ds; r <= v.de; r++) {
    var row = rowAt_(T, r);
    if (isHeld_(v, row)) continue;
    var cur = T.fmt[r - 1];
    while (cur.length < T.width) cur.push(blankFmt_());
    var style = cur.map(function (f) { return overlay_(f, base); });
    for (var i = 0; i < rowRules.length; i++) {
      if (evalCond_(rowRules[i].when, row, v, R, null)) {
        var rr = rowRules[i];
        style = style.map(function (f) { return overlay_(f, rr); });
      }
    }
    for (var j = 0; j < cellRules.length; j++) {
      var idx = cellCols[j];
      if (idx >= 0 && evalCond_(cellRules[j].when, row, v, R, idx)) style[idx] = overlay_(style[idx], cellRules[j]);
    }
    if (!fmtRowEq_(style, cur)) {
      T.fmt[r - 1] = style;
      T.fmtDirty[r - 1] = true;
      changed++;
    }
  }
  return changed;
}

function evalFormat_(R, rule, scope, plan) {
  resolveTabs_(R, rule.tabs, null, plan.warnings).forEach(function (name) {
    if (!inScope_(scope, name)) return;
    var T = loadTab_(R, name);
    var n = formatView_(R, rule, tabView_(R, T, T.extent, false), plan.warnings);
    if (n) {
      T.changed = true;
      addStats_(plan, name, n);
    }
  });
}

// ---- consolidate ---------------------------------------------------------

function findRule_(cfg, id) {
  for (var i = 0; i < cfg.rules.length; i++) if (cfg.rules[i].id === id) return cfg.rules[i];
  return null;
}

function evalConsolidate_(R, rule, plan, consolidations) {
  var cfg = R.cfg;
  var views = resolveTabs_(R, rule.sources, null, plan.warnings).map(function (name) {
    return tabView_(R, loadTab_(R, name), 0, false);
  });
  var master = [];
  var pos = Object.create(null);
  views.forEach(function (v) {
    v.all.forEach(function (pair) {
      if (!(pair[1] in pos)) { pos[pair[1]] = master.length; master.push(v.display[pair[1]]); }
    });
  });
  var derived = rule.derived || [];
  derived.forEach(function (d) {
    var key = canonKey_(d.name);
    if (!(key in pos)) { pos[key] = master.length; master.push(d.name); }
  });
  var prepend = rule.prepend_columns || [];
  var headers = prepend.map(function (p) { return p.name; }).concat(master);

  var rows = [];
  views.forEach(function (v) {
    for (var r = v.ds; r <= v.de; r++) {
      var row = rowAt_(v.T, r);
      if (rowIsEmpty_(row) || isHeld_(v, row)) continue;
      var out = blankRow_(master.length);
      v.all.forEach(function (pair) {
        var dst = pos[pair[1]];
        if (isEmpty_(out[dst])) out[dst] = row[pair[0]];
      });
      derived.forEach(function (d) {
        var p = pos[canonKey_(d.name)];
        if (isEmpty_(out[p])) out[p] = v.T.name.trim();
      });
      rows.push(prepend.map(function () { return v.T.name; }).concat(out));
    }
  });

  var headerView = buildView_(cfg, headers, true);
  var sortRule = rule.sort_like ? findRule_(cfg, rule.sort_like) : null;
  if (sortRule && sortRule.action === 'sort') {
    rows = sortRows_(rows, buildComparators_(R, sortRule.keys, headerView, plan.warnings, rule.target_tab));
  }

  var pres = presentation_(rule);
  var synthetic = { name: rule.target_tab, values: [], fmt: [], width: headers.length, fmtDirty: {} };
  for (var i = 1; i < cfg.header_row; i++) synthetic.values.push(blankRow_(headers.length));
  if (cfg.header_row > 1 && pres.title && headers.length) synthetic.values[0][0] = pres.title;
  synthetic.values.push(headers.slice());
  while (synthetic.values.length < cfg.data_start_row - 1) synthetic.values.push(blankRow_(headers.length));
  rows.forEach(function (row) { synthetic.values.push(row); });
  synthetic.fmt = synthetic.values.map(function (row) { return row.map(blankFmt_); });

  var formatRule = rule.format_like ? findRule_(cfg, rule.format_like) : null;
  var formatted = false;
  if (formatRule && formatRule.action === 'format' && rows.length) {
    formatView_(R, formatRule, tabView_(R, synthetic, 0, true), plan.warnings);
    formatted = true;
  }
  var dateKey = null;
  if (sortRule && sortRule.action === 'sort') {
    sortRule.keys.forEach(function (k) { if (!dateKey && k.type === 'date') dateKey = canonKey_(k.column); });
  }
  consolidations.push({
    rule: rule, headers: headers, rows: rows, presentation: pres, dateKey: dateKey,
    fmt: formatted ? synthetic.fmt.slice(cfg.data_start_row - 1) : null,
    sources: views.map(function (v) { return v.T.name; })
  });
  addStats_(plan, rule.target_tab, rows.length);
}

function presentation_(rule) {
  var p = {};
  var given = rule.presentation || {};
  Object.keys(ENGINE.PRESENTATION).forEach(function (k) {
    p[k] = given[k] === undefined ? ENGINE.PRESENTATION[k] : given[k];
  });
  return p;
}

// ---- move / copy ---------------------------------------------------------

function keyOf_(row, v, columns) {
  var out = [];
  for (var i = 0; i < columns.length; i++) {
    var idx = viewCol_(v, columns[i]);
    if (idx < 0) return null;
    out.push(normText_(row[idx]));
  }
  return out;
}

function keyUsable_(key) { return key !== null && key.some(function (k) { return k !== ''; }); }

function evalTransfer_(R, rule, scope, plan) {
  var cfg = R.cfg;
  if (!hasOwn_(cfg.schema_hashes, rule.to_tab) || !R.ss.getSheetByName(rule.to_tab)) {
    throw abort_("target tab '" + rule.to_tab + "' missing or not governed");
  }
  var target = loadTab_(R, rule.to_tab);
  var dst = tabView_(R, target, 0, false);
  var isCopy = rule.action === 'copy';
  var existing = {};
  if (isCopy) {
    if (rule.key_columns.some(function (c) { return viewCol_(dst, c) < 0; })) {
      throw abort_("target tab '" + rule.to_tab + "' lacks key columns");
    }
    for (var r0 = dst.ds; r0 <= dst.de; r0++) {
      var k0 = keyOf_(rowAt_(target, r0), dst, rule.key_columns);
      if (keyUsable_(k0)) existing[JSON.stringify(k0)] = true;
    }
  }
  var exclude = {};
  exclude[rule.to_tab] = true;
  var incoming = [];
  var removals = [];
  resolveTabs_(R, rule.tabs, exclude, plan.warnings).forEach(function (name) {
    if (!inScope_(scope, name)) return;
    var T = loadTab_(R, name);
    var src = tabView_(R, T, 0, false);
    var taken = [];
    for (var r = src.ds; r <= src.de; r++) {
      var row = rowAt_(T, r);
      if (rowIsEmpty_(row) || isHeld_(src, row) || !evalCond_(rule.when, row, src, R, null)) continue;
      if (isCopy) {
        var k = keyOf_(row, src, rule.key_columns);
        if (!keyUsable_(k)) {
          plan.warnings.push("tab '" + name + "' row " + r + ': empty copy key; skipped');
          continue;
        }
        var ks = JSON.stringify(k);
        if (existing[ks]) continue;
        existing[ks] = true;
      }
      incoming.push(toTarget_(row, src, dst, target));
      taken.push(r);
    }
    if (taken.length && !isCopy) removals.push({ T: T, rows: taken });
  });
  // Evaluation finished without abort: now mutate the model.
  removals.forEach(function (rm) {
    deleteRows_(rm.T, rm.rows);
    addStats_(plan, rm.T.name, rm.rows.length);
  });
  if (incoming.length) {
    var at = rule.position === 'top' ? dst.ds : dst.de + 1;
    insertRows_(target, at, incoming);
    addStats_(plan, target.name, incoming.length);
  }
}

function toTarget_(row, src, dst, target) {
  var out = blankRow_(target.width);
  src.all.forEach(function (pair) {
    var value = row[pair[0]];
    if (isEmpty_(value)) return;
    var t = pair[1] in dst.cols ? dst.cols[pair[1]] : -1;
    if (t < 0) {
      throw abort_("tab '" + src.T.name + "' column '" + (src.display[pair[1]] || pair[1]) +
        "' has no match in target '" + target.name + "'; refusing to drop data");
    }
    if (isEmpty_(out[t])) out[t] = value;
  });
  return out;
}

// ---- dedupe / clear / validate -------------------------------------------

function evalDedupe_(R, rule, scope, plan) {
  resolveTabs_(R, rule.tabs, null, plan.warnings).forEach(function (name) {
    if (!inScope_(scope, name)) return;
    var T = loadTab_(R, name);
    var v = tabView_(R, T, 0, false);
    if (rule.key_columns.some(function (c) { return viewCol_(v, c) < 0; })) {
      plan.warnings.push("tab '" + name + "': missing dedupe key column; tab skipped");
      return;
    }
    var candidates = [];
    for (var r = v.ds; r <= v.de; r++) {
      var row = rowAt_(T, r);
      if (rowIsEmpty_(row) || isHeld_(v, row)) continue;
      var key = keyOf_(row, v, rule.key_columns);
      if (keyUsable_(key)) candidates.push({ r: r, key: JSON.stringify(key) });
    }
    if (rule.keep === 'last') candidates.reverse();
    var seen = {};
    var drop = [];
    candidates.forEach(function (c) {
      if (seen[c.key]) drop.push(c.r);
      seen[c.key] = true;
    });
    if (drop.length) {
      deleteRows_(T, drop);
      addStats_(plan, name, drop.length);
    }
  });
}

function evalClear_(R, rule, scope, plan) {
  resolveTabs_(R, rule.tabs, null, plan.warnings).forEach(function (name) {
    if (!inScope_(scope, name)) return;
    var T = loadTab_(R, name);
    var v = tabView_(R, T, 0, false);
    var idxs = [];
    rule.columns.forEach(function (c) {
      var i = viewCol_(v, c);
      if (i < 0) plan.warnings.push("tab '" + name + "': clear column '" + c + "' missing; skipped");
      else idxs.push(i);
    });
    var cleared = 0;
    for (var r = v.ds; r <= v.de; r++) {
      var row = rowAt_(T, r);
      if (rowIsEmpty_(row) || isHeld_(v, row) || !evalCond_(rule.when, row, v, R, null)) continue;
      if (!idxs.some(function (i) { return !isEmpty_(row[i]); })) continue;
      var copy = row.slice();
      idxs.forEach(function (i) { copy[i] = ''; });
      T.values[r - 1] = copy;
      cleared++;
    }
    if (cleared) {
      T.changed = true;
      addStats_(plan, name, cleared);
    }
  });
}

function evalValidate_(R, rule, scope, plan) {
  var values = rule.from_enum != null
    ? R.cfg.enums[rule.from_enum].stages.map(function (s) { return s.value; })
    : rule.values.slice();
  var allowed = {};
  values.forEach(function (x) { allowed[normText_(x)] = true; });
  var dv = { values: values, allowInvalid: !!rule.allow_invalid };
  resolveTabs_(R, rule.tabs, null, plan.warnings).forEach(function (name) {
    if (!inScope_(scope, name)) return;
    var T = loadTab_(R, name);
    var v = tabView_(R, T, 0, false);
    var idx = viewCol_(v, rule.column);
    if (idx < 0) {
      plan.warnings.push("tab '" + name + "': validate column '" + rule.column + "' missing; skipped");
      return;
    }
    var invalid = 0;
    var count = 0;
    for (var r = v.ds; r <= v.de; r++) {
      var row = rowAt_(T, r);
      if (isHeld_(v, row)) continue;
      T.validations.push({ row: r, col: idx + 1, dv: dv });
      count++;
      if (!isEmpty_(row[idx]) && !allowed[normText_(row[idx])]) invalid++;
    }
    if (invalid) {
      plan.warnings.push("tab '" + name + "': " + invalid + " existing value(s) in '" + rule.column + "' not in list (left as-is)");
    }
    if (count) {
      T.changed = true;
      plan.tabs[name] = true;
    }
  });
}

/* =====================================================================
 * Writes (batched range ops only)
 * ===================================================================== */

function cellEq_(a, b) {
  if (isEmpty_(a) && isEmpty_(b)) return true;
  if (isDate_(a) || isDate_(b)) return isDate_(a) && isDate_(b) && a.getTime() === b.getTime();
  return a === b;
}

function rowEq_(a, b, width) {
  for (var i = 0; i < width; i++) if (!cellEq_(a ? a[i] : '', b ? b[i] : '')) return false;
  return true;
}

/** Group sorted row numbers into [start, count] runs. */
function runsOf_(rows) {
  var runs = [];
  rows.forEach(function (r) {
    var last = runs[runs.length - 1];
    if (last && last[0] + last[1] === r) last[1]++;
    else runs.push([r, 1]);
  });
  return runs;
}

function outValue_(v) { return v === null || v === undefined ? '' : v; }

function commitTab_(R, T) {
  var sh = T.sheet;
  T.ops.forEach(function (op) {
    if (op.del) {
      runsOf_(op.del.slice().sort(function (a, b) { return a - b; })).reverse().forEach(function (run) {
        sh.deleteRows(run[0], run[1]);
      });
    } else if (!op.append) {
      sh.insertRowsBefore(op.ins, op.count);
    }
  });

  var height = T.values.length;
  var width = T.width;
  if (height > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), height - sh.getMaxRows());
  if (width > sh.getMaxColumns()) sh.insertColumnsAfter(sh.getMaxColumns(), width - sh.getMaxColumns());

  var ds = R.cfg.data_start_row;
  var valueRows = [];
  var end = Math.max(height, T.shadow.length);
  for (var r = ds; r <= end; r++) {
    if (!rowEq_(T.values[r - 1], T.shadow[r - 1], width)) valueRows.push(r);
  }
  runsOf_(valueRows).forEach(function (run) {
    var block = [];
    for (var i = 0; i < run[1]; i++) {
      var src = T.values[run[0] - 1 + i] || [];
      var row = [];
      for (var c = 0; c < width; c++) row.push(outValue_(src[c]));
      block.push(row);
    }
    sh.getRange(run[0], 1, run[1], width).setValues(block);
  });

  var fmtRows = Object.keys(T.fmtDirty).map(Number).map(function (i) { return i + 1; })
    .filter(function (r) { return r >= ds && r <= height; })
    .sort(function (a, b) { return a - b; });
  runsOf_(fmtRows).forEach(function (run) {
    var fonts = [], lines = [], bgs = [];
    for (var i = 0; i < run[1]; i++) {
      var f = T.fmt[run[0] - 1 + i];
      var fr = [], lr = [], br = [];
      for (var c = 0; c < width; c++) {
        var cell = f[c] || blankFmt_();
        fr.push(cell.f);
        lr.push(cell.s ? 'line-through' : 'none');
        br.push(cell.b);
      }
      fonts.push(fr); lines.push(lr); bgs.push(br);
    }
    var rng = sh.getRange(run[0], 1, run[1], width);
    rng.setFontColors(fonts);
    rng.setFontLines(lines);
    rng.setBackgrounds(bgs);
  });

  writeValidations_(sh, T.validations);
}

function writeValidations_(sh, list) {
  if (!list.length) return;
  var groups = {};
  list.forEach(function (v) {
    var key = v.col + '|' + JSON.stringify(v.dv);
    (groups[key] = groups[key] || { col: v.col, dv: v.dv, rows: [] }).rows.push(v.row);
  });
  Object.keys(groups).forEach(function (k) {
    var g = groups[k];
    var rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(g.dv.values, true)
      .setAllowInvalid(g.dv.allowInvalid)
      .build();
    var rows = g.rows.filter(function (r, i, a) { return a.indexOf(r) === i; }).sort(function (a, b) { return a - b; });
    runsOf_(rows).forEach(function (run) {
      sh.getRange(run[0], g.col, run[1], 1).setDataValidation(rule);
    });
  });
}

/** Regenerate a consolidate target (legacy writeConsolidated, generalized). */
function writeConsolidated_(R, c) {
  var cfg = R.cfg;
  var ss = R.ss;
  var name = c.rule.target_tab;
  var pres = c.presentation;
  var sh = ss.getSheetByName(name);
  var created = false;
  if (!sh) {
    sh = ss.insertSheet(name, 0);
    created = true;
  }
  var nCols = c.headers.length;
  var nRows = c.rows.length;
  var hr = cfg.header_row;
  var ds = cfg.data_start_row;

  var needRows = Math.max(ds - 1 + nRows, hr);
  if (needRows > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), needRows - sh.getMaxRows());
  if (nCols > sh.getMaxColumns()) sh.insertColumnsAfter(sh.getMaxColumns(), nCols - sh.getMaxColumns());

  sh.getRange(1, 1, hr, sh.getMaxColumns()).breakApart();
  sh.clear();
  sh.getBandings().forEach(function (b) { b.remove(); });
  if (!nCols) return;

  if (hr > 1 && pres.title) {
    sh.getRange(1, 1).setValue(pres.title);
    sh.getRange(1, 1, 1, nCols).merge()
      .setFontFamily('Arial').setFontSize(14).setFontWeight('bold')
      .setHorizontalAlignment('center').setVerticalAlignment('middle')
      .setBackground(pres.title_background).setFontColor(pres.title_font);
    sh.setRowHeight(1, 32);
  }

  sh.getRange(hr, 1, 1, nCols).setValues([c.headers])
    .setFontFamily('Arial').setFontWeight('bold')
    .setHorizontalAlignment('center').setVerticalAlignment('middle')
    .setWrap(true).setBackground(pres.header_background).setFontColor(pres.header_font);

  if (nRows) {
    var dataRange = sh.getRange(ds, 1, nRows, nCols);
    dataRange.setValues(c.rows.map(function (row) { return row.map(outValue_); }))
      .setFontFamily('Arial').setFontSize(10)
      .setVerticalAlignment('top').setWrap(true);
    if (pres.banding) dataRange.applyRowBanding(SpreadsheetApp.BandingTheme[pres.banding], false, false);
    if (c.fmt) {
      dataRange.setFontColors(c.fmt.map(function (row) { return row.map(function (f) { return f.f; }); }));
      dataRange.setFontLines(c.fmt.map(function (row) {
        return row.map(function (f) { return f.s ? 'line-through' : 'none'; });
      }));
      dataRange.setBackgrounds(c.fmt.map(function (row) { return row.map(function (f) { return f.b; }); }));
    }
    var dateFmt = pres.date_format === 'match_source' ? sourceDateFormat_(R, c.sources, c.dateKey) : pres.date_format;
    for (var dc = 0; dc < nCols; dc++) {
      if (String(c.headers[dc]).toUpperCase().indexOf('DATE') !== -1) {
        sh.getRange(ds, dc + 1, nRows, 1).setNumberFormat(dateFmt);
      }
    }
  }

  sh.getRange(1, 1, Math.max(ds - 1 + nRows, hr), nCols)
    .setBorder(true, true, true, true, true, true, '#999999', SpreadsheetApp.BorderStyle.SOLID);
  sh.setFrozenRows(hr);

  if (created || R.forceWidths) {
    for (var col = 1; col <= nCols; col++) {
      var key = canonKey_(c.headers[col - 1]);
      var widths = pres.column_widths || {};
      var w = hasOwn_(widths, key) ? widths[key] : null;
      if (w === null) {
        Object.keys(widths).forEach(function (k) { if (canonKey_(k) === key) w = widths[k]; });
      }
      sh.setColumnWidth(col, w === null ? pres.default_column_width : w);
    }
  }

  var existing = sh.getProtections(SpreadsheetApp.ProtectionType.SHEET);
  if (c.rule.lock !== false) {
    if (!existing.length) lockSheet_(sh, 'Auto-generated by automation rule "' + c.rule.id + '" — locked');
  } else {
    existing.forEach(function (p) { p.remove(); });
  }
}

/**
 * 'match_source': number format of the first data cell of the date column in the first source
 * tab that has data. The date column is the sort_like rule's first `type: date` key; without
 * one, the first canonical header containing DATE.
 */
function sourceDateFormat_(R, sources, dateKey) {
  for (var s = 0; s < sources.length; s++) {
    var T = R.tabs[sources[s]];
    if (!T || T.before.height < R.cfg.data_start_row) continue;
    var v = buildView_(R.cfg, T.values[R.cfg.header_row - 1] || [], false);
    var idx = -1;
    if (dateKey && dateKey in v.cols) {
      idx = v.cols[dateKey];
    } else if (!dateKey) {
      for (var i = 0; i < v.all.length && idx < 0; i++) {
        if (v.all[i][1].indexOf('DATE') !== -1) idx = v.all[i][0];
      }
    }
    if (idx >= 0) return T.sheet.getRange(R.cfg.data_start_row, idx + 1).getNumberFormat();
  }
  return 'd/m/yyyy';
}

/** Collaborators lose edit rights; owner/script keep them. Only for consolidate targets. */
function lockSheet_(sh, desc) {
  var prot = sh.protect().setDescription(desc);
  var editors = prot.getEditors().map(function (e) { return e.getEmail(); });
  if (editors.length) prot.removeEditors(editors);
  if (prot.canDomainEdit()) prot.setDomainEdit(false);
}

/* =====================================================================
 * Snapshots & undo (invariant 6)
 * ===================================================================== */

function encodeCell_(v) { return isDate_(v) ? { $d: v.getTime() } : v; }
function decodeCell_(v) { return v && typeof v === 'object' && '$d' in v ? new Date(v.$d) : v; }

function encodeState_(state) {
  var body = {
    height: state.height, width: state.width,
    values: state.values.map(function (r) { return r.map(encodeCell_); }),
    fmt: state.fmt.map(function (r) { return r.map(function (f) { return [f.f, f.b, f.s ? 1 : 0]; }); })
  };
  var gz = Utilities.gzip(Utilities.newBlob(JSON.stringify(body), 'application/json', 'snapshot.json'));
  return Utilities.base64Encode(gz.getBytes());
}

function decodeState_(b64) {
  var blob = Utilities.newBlob(Utilities.base64Decode(b64), 'application/x-gzip', 'snapshot.json.gz');
  var body = JSON.parse(Utilities.ungzip(blob).getDataAsString());
  return {
    height: body.height, width: body.width,
    values: body.values.map(function (r) { return r.map(decodeCell_); }),
    fmt: body.fmt.map(function (r) { return r.map(function (f) { return { f: f[0], b: f[1], s: !!f[2] }; }); })
  };
}

function snapshotSheet_(ss) {
  var sh = ss.getSheetByName(ENGINE.SNAPSHOT_TAB);
  if (!sh) {
    sh = ss.insertSheet(ENGINE.SNAPSHOT_TAB, ss.getSheets().length);
    sh.getRange(1, 1, 1, 5).setValues([['run_id', 'created_at', 'tab', 'range_a1', 'parts']]);
    sh.hideSheet();
  }
  return sh;
}

function writeSnapshots_(R, names) {
  var sh = snapshotSheet_(R.ss);
  var rows = [];
  var created = new Date().toISOString();
  names.forEach(function (name) {
    var T = R.tabs[name];
    var data = encodeState_(T.before);
    var parts = [];
    for (var i = 0; i < data.length; i += ENGINE.SNAPSHOT_CELL_CHARS) parts.push(data.substr(i, ENGINE.SNAPSHOT_CELL_CHARS));
    var a1 = 'A1:' + colLetter_(Math.max(T.before.width, 1)) + Math.max(T.before.height, 1);
    rows.push([R.id, created, name, a1, parts.length].concat(parts));
  });
  var width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
  rows = rows.map(function (r) { while (r.length < width) r.push(''); return r; });
  if (width > sh.getMaxColumns()) sh.insertColumnsAfter(sh.getMaxColumns(), width - sh.getMaxColumns());
  var start = sh.getLastRow() + 1;
  if (start + rows.length - 1 > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), start + rows.length - 1 - sh.getMaxRows());
  var rng = sh.getRange(start, 1, rows.length, width);
  rng.setNumberFormat('@');
  rng.setValues(rows);
  pruneSnapshots_(sh);
  return R.id;
}

function pruneSnapshots_(sh) {
  var last = sh.getLastRow();
  if (last < 2) return;
  var ids = sh.getRange(2, 1, last - 1, 1).getValues().map(function (r) { return String(r[0]); });
  var distinct = [];
  ids.forEach(function (id) { if (distinct.indexOf(id) === -1) distinct.push(id); });
  if (distinct.length <= ENGINE.SNAPSHOT_KEEP_RUNS) return;
  var keep = distinct.slice(-ENGINE.SNAPSHOT_KEEP_RUNS);
  var firstKept = ids.findIndex(function (id) { return keep.indexOf(id) !== -1; });
  if (firstKept > 0) sh.deleteRows(2, firstKept);
}

function readSnapshots_(runId) {
  var sh = SpreadsheetApp.getActive().getSheetByName(ENGINE.SNAPSHOT_TAB);
  if (!sh || sh.getLastRow() < 2) return [];
  var data = sh.getRange(2, 1, sh.getLastRow() - 1, sh.getLastColumn()).getValues();
  var out = [];
  data.forEach(function (row) {
    if (String(row[0]) !== runId) return;
    var n = Number(row[4]);
    out.push({ tab: String(row[2]), body: decodeState_(row.slice(5, 5 + n).join('')) });
  });
  return out;
}

function lastSnapshotRunId_() {
  var sh = SpreadsheetApp.getActive().getSheetByName(ENGINE.SNAPSHOT_TAB);
  if (!sh || sh.getLastRow() < 2) return null;
  return String(sh.getRange(sh.getLastRow(), 1).getValue());
}

/** Write a captured state back over the tab's rows (values + font colour/line + fill). */
function restoreTab_(sh, state) {
  var current = sh.getLastRow();
  if (state.height > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), state.height - sh.getMaxRows());
  var width = Math.max(state.width, 1);
  if (state.height > 0 && state.width > 0) {
    var rng = sh.getRange(1, 1, state.height, state.width);
    rng.setValues(state.values.map(function (r) { return r.map(outValue_); }));
    rng.setFontColors(state.fmt.map(function (r) { return r.map(function (f) { return f.f; }); }));
    rng.setFontLines(state.fmt.map(function (r) { return r.map(function (f) { return f.s ? 'line-through' : 'none'; }); }));
    rng.setBackgrounds(state.fmt.map(function (r) { return r.map(function (f) { return f.b; }); }));
  }
  if (current > state.height) {
    sh.getRange(state.height + 1, 1, current - state.height, Math.max(width, sh.getLastColumn())).clearContent();
  }
}

function colLetter_(n) {
  var s = '';
  while (n > 0) {
    var rem = (n - 1) % 26;
    s = String.fromCharCode(65 + rem) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/* =====================================================================
 * Config storage (Script Properties = source of truth; _config tab = mirror)
 * ===================================================================== */

var CONFIG_CACHE_ = null;

function props_() { return PropertiesService.getScriptProperties(); }

function writeChunked_(key, text) {
  var p = props_();
  var old = Number(p.getProperty(key + '.n') || 0);
  var map = {};
  var n = 0;
  var i = 0;
  while (i < text.length) {
    var end = Math.min(i + ENGINE.CHUNK_CHARS, text.length);
    var code = text.charCodeAt(end - 1);
    if (end < text.length && code >= 0xD800 && code <= 0xDBFF) end--;  // never split a surrogate pair
    map[key + '.' + n] = text.slice(i, end);
    n++;
    i = end;
  }
  map[key + '.n'] = String(n);
  p.setProperties(map, false);
  for (var j = n; j < old; j++) p.deleteProperty(key + '.' + j);
}

function readChunked_(key) {
  var p = props_();
  var n = Number(p.getProperty(key + '.n') || 0);
  if (!n) return null;
  var parts = [];
  for (var i = 0; i < n; i++) {
    var part = p.getProperty(key + '.' + i);
    if (part === null) throw new Error('missing chunk ' + i + ' of ' + key);
    parts.push(part);
  }
  return parts.join('');
}

function readJsonProp_(key, fallback) {
  var raw = props_().getProperty(key);
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch (err) {
    logError_('readJsonProp ' + key, err);
    return fallback;
  }
}

function loadConfig_() {
  if (CONFIG_CACHE_) return CONFIG_CACHE_;
  var text = readChunked_(ENGINE.KEY_CONFIG);
  if (!text) return null;
  var sha = props_().getProperty(ENGINE.KEY_CONFIG_SHA);
  if (sha && sha !== sha256Hex_(text)) throw new Error('stored config is corrupt (checksum mismatch)');
  CONFIG_CACHE_ = JSON.parse(text);
  return CONFIG_CACHE_;
}

/** Minimal shape check. The backend validator is authoritative; this only refuses garbage. */
function checkConfigShape_(cfg) {
  var errors = [];
  function err(pointer, code, message) { errors.push({ pointer: pointer, code: code, message: message }); }
  if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) {
    err('', 'schema.type', 'config must be an object');
    return errors;
  }
  if (!(cfg.config_version >= 1)) err('/config_version', 'schema.missing', 'config_version must be >= 1');
  if (!cfg.schema_hashes || typeof cfg.schema_hashes !== 'object' || !Object.keys(cfg.schema_hashes).length) {
    err('/schema_hashes', 'schema.missing', 'schema_hashes must be a non-empty object');
  }
  if (!(cfg.header_row >= 1)) err('/header_row', 'schema.missing', 'header_row must be >= 1');
  if (!(cfg.data_start_row > cfg.header_row)) err('/data_start_row', 'data_start_before_header', 'data_start_row must be after header_row');
  if (!Array.isArray(cfg.rules) || !cfg.rules.length) {
    err('/rules', 'schema.missing', 'rules must be a non-empty array');
    return errors;
  }
  var ids = {};
  cfg.rules.forEach(function (r, i) {
    if (!r || typeof r.id !== 'string') err('/rules/' + i + '/id', 'schema.missing', 'rule id required');
    else if (ids[r.id]) err('/rules/' + i + '/id', 'duplicate_rule_id', 'duplicate rule id ' + r.id);
    else ids[r.id] = true;
    if (!r || ENGINE.ACTIONS.indexOf(r.action) === -1) err('/rules/' + i, 'unknown_action', 'unknown action ' + (r && r.action));
    if (!r || !r.trigger || typeof r.trigger !== 'object') err('/rules/' + i + '/trigger', 'schema.missing', 'trigger required');
  });
  return errors;
}

function mirrorConfig_(cfg) {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(ENGINE.CONFIG_TAB);
  if (!sh) {
    sh = ss.insertSheet(ENGINE.CONFIG_TAB, ss.getSheets().length);
    sh.hideSheet();
  }
  sh.clear();
  var lines = ['READ-ONLY MIRROR — the engine reads its config from Script Properties, not from this tab.']
    .concat(JSON.stringify(cfg, null, 2).split('\n'));
  if (lines.length > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), lines.length - sh.getMaxRows());
  var rng = sh.getRange(1, 1, lines.length, 1);
  rng.setNumberFormat('@');
  rng.setValues(lines.map(function (l) { return [l]; }));
}

function getStatus_() {
  return props_().getProperty(ENGINE.KEY_STATUS) || 'ACTIVE';
}

function setStatus_(status, reason) {
  var before = getStatus_();
  props_().setProperties(mapOf_(ENGINE.KEY_STATUS, status, ENGINE.KEY_STATUS_REASON, reason || ''), false);
  if (before !== status) enqueue_({ type: 'status_change', from: before, to: status, reason: reason || null });
}

/* =====================================================================
 * Phone-home log queue (invariant 7: never blocks the sheet)
 * ===================================================================== */

function readQueue_() {
  try {
    var text = readChunked_(ENGINE.KEY_QUEUE);
    return text ? JSON.parse(text) : [];
  } catch (err) {
    console.error('log queue unreadable; resetting', err);
    return [{ type: 'error', where: 'readQueue', message: errText_(err), at: new Date().toISOString() }];
  }
}

function enqueue_(record) {
  try {
    record.engine_version = ENGINE_VERSION;
    record.at = record.at || new Date().toISOString();
    var q = readQueue_();
    q.push(record);
    if (q.length > ENGINE.QUEUE_MAX) {
      var dropped = q.length - ENGINE.QUEUE_MAX;
      q = q.slice(dropped);
      var total = Number(props_().getProperty(ENGINE.KEY_QUEUE_DROPPED) || 0) + dropped;
      props_().setProperty(ENGINE.KEY_QUEUE_DROPPED, String(total));
    }
    writeChunked_(ENGINE.KEY_QUEUE, JSON.stringify(q));
  } catch (err) {
    console.error('enqueue failed', err, JSON.stringify(record));  // last resort: execution log
  }
}

function logError_(where, err) {
  console.error(where, err);
  enqueue_({ type: 'error', where: where, message: errText_(err), stack: String(err && err.stack || '').slice(0, 2000) });
}

function errText_(err) { return String(err && err.message || err); }

/** Best effort. Returns true when the queue is empty afterwards. */
function flush_(force) {
  var p = props_();
  var q = readQueue_();
  if (!q.length) return true;
  var url = p.getProperty(ENGINE.KEY_WEBHOOK_URL);
  if (!url) return false;
  var failedAt = Number(p.getProperty(ENGINE.KEY_FLUSH_FAILED_AT) || 0);
  if (!force && failedAt && Date.now() - failedAt < ENGINE.FLUSH_BACKOFF_MS) return false;
  var batch = q.slice(0, ENGINE.FLUSH_BATCH);
  var cfg = null;
  try { cfg = loadConfig_(); } catch (err) { logError_('flush: config unreadable', err); }
  var secret = p.getProperty(ENGINE.KEY_WEBHOOK_SECRET) || '';
  var payload = {
    sheet_id: cfg ? cfg.sheet_id : SpreadsheetApp.getActive().getId(),
    org_id: cfg ? cfg.org_id : null,
    engine_version: ENGINE_VERSION,
    config_version: cfg ? cfg.config_version : null,
    status: getStatus_(),
    dropped: Number(p.getProperty(ENGINE.KEY_QUEUE_DROPPED) || 0),
    flush_failures: Number(p.getProperty(ENGINE.KEY_FLUSH_FAILURES) || 0),
    last_flush_error: p.getProperty(ENGINE.KEY_FLUSH_LAST_ERROR),
    secret: secret,
    runs: batch
  };
  try {
    var resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      headers: { 'X-Engine-Secret': secret },
      muteHttpExceptions: true
    });
    var code = resp.getResponseCode();
    if (code < 200 || code >= 300) throw new Error('HTTP ' + code);
  } catch (err) {
    // Reported on the next successful flush (counter + message) rather than as a queue record,
    // so an outage cannot grow the queue by itself.
    var failures = Number(p.getProperty(ENGINE.KEY_FLUSH_FAILURES) || 0) + 1;
    p.setProperties(mapOf_(ENGINE.KEY_FLUSH_FAILED_AT, String(Date.now()),
      ENGINE.KEY_FLUSH_FAILURES, String(failures),
      ENGINE.KEY_FLUSH_LAST_ERROR, errText_(err).slice(0, 500)), false);
    console.error('log flush failed; ' + q.length + ' record(s) kept', err);
    return false;
  }
  var rest = q.slice(batch.length);
  writeChunked_(ENGINE.KEY_QUEUE, JSON.stringify(rest));
  [ENGINE.KEY_FLUSH_FAILED_AT, ENGINE.KEY_QUEUE_DROPPED, ENGINE.KEY_FLUSH_FAILURES, ENGINE.KEY_FLUSH_LAST_ERROR]
    .forEach(function (k) { p.deleteProperty(k); });
  return rest.length === 0;
}

/* =====================================================================
 * Misc
 * ===================================================================== */

function sha256Hex_(text) {
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8);
  return bytes.map(function (b) { var h = ((b + 256) % 256).toString(16); return h.length === 1 ? '0' + h : h; }).join('');
}

function notify_(message) {
  try {
    SpreadsheetApp.getActive().toast(String(message), 'Automation', 5);
  } catch (err) {
    logError_('toast', err);
  }
}

function mapOf_() {
  var m = {};
  for (var i = 0; i + 1 < arguments.length; i += 2) m[arguments[i]] = arguments[i + 1];
  return m;
}
