// ---------------------------------------------------------------- running the rules

/**
 * Runs every rule now, in order. This is the "Run now" menu item.
 *
 * Nothing is touched until every governed tab's header row still matches the fingerprint taken
 * when this script was generated. If any of them changed, the whole run stops and says so,
 * rather than reorganising some tabs against names the rules no longer understand.
 *
 * The rules are worked out in memory first and written to the sheet only once all of them have
 * succeeded, so a rule that fails (or would move too many rows) leaves the sheet untouched.
 */
function ${entry_point}() {
  runEvent_({ kind: 'manual' }, true);
}

/** One run, one at a time: a second run waits for the first rather than interleaving with it. */
function runEvent_(event, loud) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    report_(['another run was still going, so this one was skipped - use Run now in a moment'], true);
    return false;
  }
  try {
    runLocked_(event, loud);
  } finally {
    lock.releaseLock();
  }
  return true;
}

function runLocked_(event, loud) {
  var ss = SpreadsheetApp.getActive();
  var edited = null;
  if (event.kind === 'edit') {
    // Edits to tabs this script does not govern, or to a tab it builds itself, start nothing.
    if (!isGoverned_(event.tab) || CONSOLIDATE_TARGETS.indexOf(event.tab) >= 0) return;
    edited = buildView_(ss.getSheetByName(event.tab));
  }
  var chosen = selectRules_(event, edited);
  if (!chosen.steps.length && !chosen.dirty.length) {
    if (loud) report_([], true);
    return;
  }

  var run = startRun_(ss);                     // pre-flight: every header row still as generated
  if (!run) return;
  markDirty_(chosen.dirty);
  if (event.kind === 'debounced') clearDirty_(event.ruleIds);

  for (var s = 0; s < chosen.steps.length; s++) {
    chosen.steps[s].rule.step(run, chosen.steps[s].scope);
  }
  if (run.failed) {
    run.notes.push('nothing was changed');
    report_(run.notes, true);
    return;
  }
  if (run.backup.length) writeBackup_(ss, run.backup);   // before anything is removed or overwritten
  for (var w = 0; w < run.writes.length; w++) run.writes[w]();
  report_(run.notes, loud);
}

/** Checks every governed tab's header row, then reads the tabs in the order they appear. */
function startRun_(ss) {
  var notes = [], found = {};
  for (var t = 0; t < GOVERNED_TABS.length; t++) {
    var tabName = GOVERNED_TABS[t].tab;
    var sheet = ss.getSheetByName(tabName);
    if (!sheet) {
      notes.push(tabName + ': tab not found');
      continue;
    }
    var checking = buildView_(sheet);
    if (headerHash_(checking) !== GOVERNED_TABS[t].headerHash) {
      notes.push(tabName + ': the header row changed since this script was generated');
      continue;
    }
    found[tabName] = checking;
  }
  if (notes.length) {                          // pre-flight failed: nothing runs at all
    notes.push('nothing was changed - regenerate the script for the sheet as it is now');
    report_(notes, true);
    return null;
  }
  var views = [], sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (found[sheets[i].getName()]) views.push(found[sheets[i].getName()]);
  }
  return {
    ss: ss, today: todayStart_(ss), views: views, notes: [], writes: [], backup: [], guardedRows: 0,
    failed: false,
    fail: function (note) { this.failed = true; this.notes.push(note); }
  };
}

function isGoverned_(tabName) {
  for (var t = 0; t < GOVERNED_TABS.length; t++) if (GOVERNED_TABS[t].tab === tabName) return true;
  return false;
}

/** A toast in the sheet. Automatic runs stay quiet unless there is something to look at. */
function report_(notes, loud) {
  var ss = SpreadsheetApp.getActive();
  if (!notes.length) {
    if (loud) ss.toast('Finished. Everything is organised.', PRODUCT, 5);
    return;
  }
  ss.toast(notes.join('  |  '), PRODUCT + ' - ' + notes.length + ' thing(s) to look at', 30);
}
