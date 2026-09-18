'use strict';
/**
 * M2 exit test: Engine.gs + reference config must reproduce legacy.gs exactly
 * (sort order, fonts, strikethrough, overdue tint, freeze text, SUMMARY output),
 * plus the CLAUDE.md engine rules (pre-flight pause, hold rows, snapshots/undo,
 * rollback, guards, non-blocking logging, batched writes, no lock-outs).
 *
 * Run: node --test engine/test
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createEnv, visibleTabs } = require('./harness');

const FIX = path.join(__dirname, 'fixtures');
const WORKBOOK = JSON.parse(fs.readFileSync(path.join(FIX, 'reference_workbook.json'), 'utf8'));
const CONFIG = JSON.parse(fs.readFileSync(path.join(FIX, 'reference_config.json'), 'utf8'));
const NOW = WORKBOOK.today + 'T10:00:00';
const clone = (x) => JSON.parse(JSON.stringify(x));

/* ------------------------------------------------------------------ helpers */

function legacyEnv(workbook = WORKBOOK) {
  const env = createEnv({ script: 'legacy', now: NOW });
  env.load(workbook);
  env.call('installTrigger');
  return env;
}

function engineEnv(workbook = WORKBOOK, config = CONFIG, { run = true, options } = {}) {
  const env = createEnv({ script: 'engine', now: NOW });
  env.load(workbook);
  const res = JSON.parse(env.call('engineSetConfig', JSON.stringify(config), options));
  assert.equal(res.ok, true, JSON.stringify(res.errors));
  env.call('engineInstall');
  if (run) env.call('engineRunAll');
  return env;
}

function rawEnv(workbook = WORKBOOK) {
  const env = createEnv({ script: 'engine', now: NOW });
  env.load(workbook);
  return env;
}

function tab(env, name) {
  return env.dump().find((t) => t.name === name);
}

function col(env, tabName, header, headerRow = 2) {
  const t = tab(env, tabName);
  const idx = t.cells[headerRow - 1].findIndex((c) => c.v === header);
  return t.cells.slice(headerRow).map((r) => r[idx].v);
}

/** Readable first difference instead of a 5,000-line deepEqual dump. */
function firstDiff(a, b, where = '') {
  if (typeof a !== typeof b || Array.isArray(a) !== Array.isArray(b)) return `${where}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`;
  if (a && typeof a === 'object') {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of keys) {
      const d = firstDiff(a[k], b[k], `${where}/${k}`);
      if (d) return d;
    }
    return null;
  }
  return a === b ? null : `${where}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`;
}

function assertSameSheets(actualEnv, expectedEnv, names) {
  let a = visibleTabs(actualEnv.dump());
  let b = visibleTabs(expectedEnv.dump());
  if (names) {
    a = a.filter((t) => names.includes(t.name));
    b = b.filter((t) => names.includes(t.name));
  }
  assert.deepEqual(a.map((t) => t.name), b.map((t) => t.name), 'tab order');
  const diff = firstDiff(a, b);
  assert.equal(diff, null, `engine differs from legacy at ${diff}`);
}

function findRow(env, tabName, srNo) {
  const t = tab(env, tabName);
  return t.cells.findIndex((r) => r[0].v === srNo) + 1;
}

/* ------------------------------------------------------- M2 exit criterion */

test('install + run all: identical to legacy installTrigger on the reference workbook', () => {
  const legacy = legacyEnv();
  const engine = engineEnv();
  assertSameSheets(engine, legacy);
  // and the content is what the spec says (guards against mock-level agreement on nonsense)
  assert.deepEqual(col(engine, 'MACHINES', 'SR NO'), [2, 10, 4, 3, 1, 8, 7, 5, 6, 9]);
  assert.deepEqual(col(engine, 'SPARES', 'SR NO'), ['S2', 'S1', 'S3']);
  const summary = tab(engine, 'SUMMARY');
  assert.equal(summary.protected, true);
  assert.deepEqual(summary.cells[1].map((c) => c.v), [
    'SOURCE SHEET', 'SR NO', 'CUSTOMER NAME', 'MACHINE NAME', 'QTY', 'DISPATCH DATE', 'STATUS',
    'FREEZE?', 'PANEL IN-HOUSE OR OUTSOURCE?', 'Type of Work', 'INVOICE NO',
  ]);
  assert.deepEqual(summary.cells.slice(2).map((r) => r[1].v), [2, 'S2', 10, 4, 3, 'S1', 1, 8, 'S3', 7, 5, 6, 9]);
  const s1 = summary.cells.find((r) => r[1].v === 'S1');
  assert.equal(s1[9].v, 'SPARES');  // Type of Work auto-filled
  assert.equal(summary.cells[2][5].nf, 'd/m/yyyy');  // date format matched from first source
});

test('reference formatting details (fonts, strike, overdue tint, freeze text)', () => {
  const env = engineEnv();
  const m = tab(env, 'MACHINES');
  const row = (sr) => m.cells[findRow(env, 'MACHINES', sr) - 1];
  const FREEZE = 6;
  const fonts = (sr) => new Set(row(sr).filter((_, i) => i !== FREEZE).map((c) => c.fc));
  assert.deepEqual(fonts(2), new Set(['#000000']));                 // overdue -> black
  assert.deepEqual(new Set(row(2).map((c) => c.bg)), new Set(['#fce4ec']));
  assert.deepEqual(fonts(4), new Set(['#ff0000']));                 // in process, future
  assert.deepEqual(new Set(row(4).map((c) => c.bg)), new Set([null]));
  assert.deepEqual(fonts(1), new Set(['#008000']));
  assert.deepEqual(fonts(8), new Set(['#d4a017']));
  assert.deepEqual(new Set(row(5).map((c) => c.fl)), new Set(['line-through']));
  assert.equal(row(1)[FREEZE].fc, '#008000');
  assert.equal(row(4)[FREEZE].fc, '#008000');
  assert.equal(row(7)[FREEZE].fc, '#008000');                       // "yes" lower-case
  assert.equal(row(2)[FREEZE].fc, '#000000');                       // "NO"
  assert.deepEqual(new Set(row(6).map((c) => c.fc)), new Set(['#000000']));  // unknown status: neutral
});

test('edit sequence: engine matches legacy after each edit (summary after debounce)', () => {
  const legacy = legacyEnv();
  const engine = engineEnv();
  const edits = [
    ['MACHINES', 'status', 1, 6, 'Cancelled'],
    ['MACHINES', 'dispatch date', 4, 5, { $date: '2026-09-01' }],
    ['MACHINES', 'freeze', 5, 7, 'YES'],
    ['MACHINES', 'freeze cleared', 5, 7, ''],
    ['SPARES', 'status', 3, 5, 'in process'],
    ['MACHINES', 'status blank', 8, 6, ''],
    ['MACHINES', 'new row status', 13, 6, 'Disputed'],
    ['MACHINES', 'new row sr', null, 1, 11],
    ['MACHINES', 'customer (non-trigger column)', 3, 2, 'Renamed Co'],
    ['LEGENDS', 'untracked tab', 3, 2, 'whatever'],
  ];
  for (const [name, label, rowSpec, c, value] of edits) {
    // The same physical cell is edited in both (rows can differ only if engine diverged).
    const r = rowSpec === null ? tab(legacy, name).cells.findIndex((row) => row[5].v === 'Disputed' && row[0].v === '') + 1 : rowSpec;
    legacy.edit(name, r, c, value);
    engine.edit(name, r, c, value);
    const tabs = ['MACHINES', 'SPARES', 'LEGENDS'];
    assertSameSheets(engine, legacy, tabs);
    engine.advance(121);
    engine.tick();
    legacy.advance(121);
    assertSameSheets(engine, legacy);
    assert.ok(label);
  }
});

test('edits outside the trigger columns do not run the chain', () => {
  const env = engineEnv(WORKBOOK, CONFIG, { run: false });  // sheet still unsorted
  const before = tab(env, 'MACHINES');
  env.edit('MACHINES', 3, 2, 'Renamed Co');                 // CUSTOMER NAME: not a trigger column
  const after = tab(env, 'MACHINES');
  before.cells[2][1].v = 'Renamed Co';
  assert.equal(firstDiff(after, before), null, 'no sort/format for a non-trigger column');
  assert.ok(env.props()['engine.dirty.summary'], 'consolidate still marked dirty');
  env.edit('MACHINES', 3, 7, 'NO');                         // WORK ORDER FREEZE? -> FREEZE? is a trigger
  assert.deepEqual(col(env, 'MACHINES', 'SR NO'), [2, 10, 4, 3, 1, 8, 7, 5, 6, 9]);
});

test('summary rebuild is debounced: a burst of edits rebuilds once, after the quiet period', () => {
  const env = engineEnv();
  const before = tab(env, 'SUMMARY');
  env.mock.resetCalls();
  for (let i = 0; i < 10; i++) {
    env.edit('MACHINES', 3, 2, `Burst ${i}`);
    env.advance(10);
    env.tick();  // still inside the quiet window each time
  }
  assert.deepEqual(tab(env, 'SUMMARY'), before, 'summary must not rebuild during the burst');
  assert.equal(env.mock.calls.byMethod['Sheet.clear'] || 0, 0);
  env.advance(121);
  env.tick();
  assert.equal(env.mock.calls.byMethod['Sheet.clear'], 1, 'exactly one rebuild');
  const names = tab(env, 'SUMMARY').cells.map((r) => r[2].v);
  assert.ok(names.includes('Burst 9'));
  env.advance(300);
  env.tick();
  assert.equal(env.mock.calls.byMethod['Sheet.clear'], 1, 'no rebuild without new edits');
});

/* ------------------------------------------------------ randomized parity */

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6D2B79F5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const STATUSES = ['IN-PROCESS', 'In Process', 'A. IN PROCESS', 'Completed', 'COMPLETED ', 'Disputed',
  'Dispatched', 'DISPATCHED', 'Cancelled', 'cancel', '', 'On Hold', '??', 'b. completed'];
const FREEZES = ['YES', 'yes', ' YES ', 'NO', ''];
const EXTRA = ['CUSTOMER NAME', 'QTY', 'INVOICE NO', 'Type of Work', 'REMARKS', 'WORK ORDER NO'];

function randomWorkbook(seed) {
  const r = rng(seed);
  const pick = (arr) => arr[Math.floor(r() * arr.length)];
  const names = ['MACHINES', 'SPARES', 'SERVICE', 'PANELS'].filter(() => r() < 0.6);
  if (!names.length) names.push('MACHINES');
  const tabs = [{ name: 'LEGENDS', rows: [['x'], ['COLOUR', 'MEANING']] }];
  for (const name of names) {
    const cols = ['STATUS'];
    if (r() < 0.9) cols.push(pick(['DISPATCH DATE', 'TENTATIVE DISPATCH DATE']));
    if (r() < 0.8) cols.push(pick(['WORK ORDER FREEZE?', 'TECHNICAL FREEZE?']));
    for (const e of EXTRA) if (r() < 0.35) cols.push(e);
    cols.sort(() => r() - 0.5);
    const n = 2 + Math.floor(r() * 30);
    const rows = [];
    for (let i = 0; i < n; i++) {
      let row = cols.map((c) => {
        if (c === 'STATUS') return pick(STATUSES);
        if (c.includes('DISPATCH')) {
          if (r() < 0.2) return '';
          const d = new Date(2026, 8, 16 + Math.floor(r() * 41) - 20);
          return { $date: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` };
        }
        if (c.includes('FREEZE')) return pick(FREEZES);
        return pick(['', `${c.slice(0, 3)}${i}`, 1 + Math.floor(r() * 5)]);
      });
      if (r() < 0.08) row = cols.map(() => '');
      rows.push(row);
    }
    rows[n - 1][cols.indexOf('STATUS')] = pick(STATUSES.slice(0, 10));
    const dcol = cols.findIndex((c) => c.includes('DISPATCH'));
    tabs.push({
      name, rows: [[`${name} TITLE`], cols, ...rows],
      number_formats: dcol >= 0 ? [{ row: 3, col: dcol + 1, rows: n, format: pick(['d/m/yyyy', 'dd-mm-yyyy']) }] : [],
    });
  }
  return { tabs };
}

function hashesFor(env, names) {
  const out = {};
  for (const n of names) {
    const header = tab(env, n).cells[1].map((c) => c.v);
    out[n] = env.call('schemaHash_', header);
  }
  return out;
}

test('randomized parity with legacy (150 workbooks)', () => {
  for (let seed = 1; seed <= 150; seed++) {
    const wb = randomWorkbook(seed);
    const governed = wb.tabs.filter((t) => t.name !== 'LEGENDS').map((t) => t.name);
    const probe = rawEnv(wb);
    const config = clone(CONFIG);
    config.schema_hashes = hashesFor(probe, governed);
    const legacy = legacyEnv(wb);
    const engine = engineEnv(wb, config);
    const a = visibleTabs(engine.dump());
    const b = visibleTabs(legacy.dump());
    const diff = firstDiff(a, b);
    assert.equal(diff, null, `seed ${seed}: ${diff}`);
  }
});

/* ------------------------------------------------------------ invariants */

test('pre-flight: renamed header pauses the sheet before any rule runs', () => {
  const env = engineEnv();
  const snapshot = visibleTabs(env.dump());
  env.edit('MACHINES', 2, 6, 'STAGE');  // header renamed
  assert.equal(env.props()['engine.status'], 'PAUSED_DRIFT');
  const drift = env.queue().filter((r) => r.status === 'PAUSED_DRIFT');
  assert.equal(drift.length, 1);
  assert.deepEqual(drift[0].drift.map((d) => d.tab), ['MACHINES']);
  // a data edit elsewhere now changes nothing but the edited cell
  env.edit('SPARES', 3, 5, 'Cancelled');
  env.advance(200);
  env.tick();
  const after = visibleTabs(env.dump());
  const expected = clone(snapshot);
  expected.find((t) => t.name === 'MACHINES').cells[1][5].v = 'STAGE';
  expected.find((t) => t.name === 'SPARES').cells[2][4].v = 'Cancelled';
  assert.equal(firstDiff(after, expected), null);
  // nothing got protected by the pause (only the by-design SUMMARY lock)
  assert.deepEqual(after.filter((t) => t.protected).map((t) => t.name), ['SUMMARY']);
  // resume refuses while drifted, accepts once fixed
  assert.equal(env.call('engineResume'), 'PAUSED_DRIFT');
  env.edit('MACHINES', 2, 6, 'STATUS');
  assert.equal(env.call('engineResume'), 'ACTIVE');
});

test('missing governed tab is drift', () => {
  const env = engineEnv();
  env.mock.ss.deleteSheet(env.mock.ss.getSheetByName('SPARES'));
  env.edit('MACHINES', 3, 6, 'Completed');
  assert.equal(env.props()['engine.status'], 'PAUSED_DRIFT');
});

test('manual pause never locks humans out and stops rules', () => {
  const env = engineEnv();
  env.call('enginePause');
  const before = col(env, 'MACHINES', 'SR NO');
  env.edit('MACHINES', 3, 6, 'Cancelled');
  assert.deepEqual(col(env, 'MACHINES', 'SR NO'), before);
  assert.deepEqual(visibleTabs(env.dump()).filter((t) => t.protected).map((t) => t.name), ['SUMMARY']);
  env.call('engineResume');
  env.edit('MACHINES', 3, 6, 'Cancelled');
  assert.equal(col(env, 'MACHINES', 'SR NO').at(-3), 2);
});

test('!hold rows are exempt: position, values and formats untouched', () => {
  const wb = clone(WORKBOOK);
  const m = wb.tabs.find((t) => t.name === 'MACHINES');
  m.rows[1].push('!hold');
  m.rows.slice(2).forEach((r) => r.push(''));
  m.rows[2][8] = 'x';  // SR 1 (Completed) held at row 3
  m.formats = [{ row: 3, col: 1, font: '#123456', background: '#abcdef' }];
  const probe = rawEnv(wb);
  const config = clone(CONFIG);
  config.schema_hashes.MACHINES = hashesFor(probe, ['MACHINES']).MACHINES;
  const env = engineEnv(wb, config);
  const t = tab(env, 'MACHINES');
  assert.equal(t.cells[2][0].v, 1, 'held row stays at row 3');
  assert.equal(t.cells[2][0].fc, '#123456');
  assert.equal(t.cells[2][0].bg, '#abcdef');
  assert.deepEqual(col(env, 'MACHINES', 'SR NO'), [1, 2, 10, 4, 3, 8, 7, 5, 6, 9]);
  const summary = tab(env, 'SUMMARY');
  assert.ok(!summary.cells.some((r) => r[1].v === 1), 'held row not consolidated');
  assert.ok(!summary.cells[1].some((c) => c.v === '!hold'), 'hold column not consolidated');
});

test('snapshot + undo restore the exact pre-run state', () => {
  const original = rawEnv();
  const env = engineEnv();
  assert.notEqual(firstDiff(tab(env, 'MACHINES'), tab(original, 'MACHINES')), null);
  const restored = env.call('engineUndoLastRun');
  assert.deepEqual(Array.from(restored).sort(), ['MACHINES', 'SPARES']);
  for (const name of ['MACHINES', 'SPARES', 'LEGENDS']) {
    assert.equal(firstDiff(tab(env, name), tab(original, name)), null, name);
  }
  assert.equal(tab(env, '_engine_snapshots').hidden, true);
  const runs = env.queue().filter((r) => r.type === 'run');
  assert.ok(runs.every((r) => r.snapshot_ref === runs[0].run_id));
});

test('undo after an edit-triggered sort restores the post-edit, pre-sort order', () => {
  const env = engineEnv();
  env.edit('MACHINES', 3, 6, 'Cancelled');  // SR 2 was at row 3
  const sorted = col(env, 'MACHINES', 'SR NO');
  assert.notEqual(sorted[0], 2);
  env.call('engineUndoLastRun');
  const back = col(env, 'MACHINES', 'SR NO');
  assert.deepEqual(back, [2, 10, 4, 3, 1, 8, 7, 5, 6, 9]);
  assert.equal(tab(env, 'MACHINES').cells[2][5].v, 'Cancelled', 'the human edit itself is kept');
});

test('write failure mid-run rolls every tab back and logs ERROR', () => {
  const original = rawEnv();
  const env = engineEnv(WORKBOOK, CONFIG, { run: false });
  env.mock.failOn('Range.setBackgrounds', 1);  // MACHINES formats written, SPARES format write fails
  env.call('engineRunAll');
  for (const name of ['MACHINES', 'SPARES']) {
    assert.equal(firstDiff(tab(env, name), tab(original, name)), null, `${name} not rolled back`);
  }
  assert.equal(tab(env, 'SUMMARY'), undefined, 'consolidate never written after a failed commit');
  const errors = env.queue().filter((r) => r.type === 'run' && r.status === 'ERROR');
  assert.equal(errors.length, 3);
  assert.match(errors[0].error, /rolled back: \[MACHINES, SPARES\]/);
});

test('guard: max_rows_per_run refuses the whole run with zero writes', () => {
  const config = clone(CONFIG);
  config.guards.max_rows_per_run = 3;
  const original = rawEnv();
  const env = engineEnv(WORKBOOK, config, { run: false });
  env.mock.resetCalls();
  env.call('engineRunAll');
  assert.equal(env.mock.calls.writes, 0);
  assert.equal(env.mock.calls.structural, 0);
  for (const name of ['MACHINES', 'SPARES']) assert.equal(firstDiff(tab(env, name), tab(original, name)), null);
  const recs = env.queue().filter((r) => r.type === 'run');
  assert.ok(recs.every((r) => r.status === 'ERROR'));
  assert.match(recs[0].error, /max_rows_per_run exceeded/);
});

test('logging outage is invisible to the sheet; queue kept, backoff, then flushed', () => {
  const env = engineEnv(WORKBOOK, CONFIG, { run: false, options: { webhook_url: 'https://backend.example/api/v1/webhooks/log', webhook_secret: 's3cret' } });
  env.ctx.__fetchMode = 'throw';
  env.call('engineRunAll');                    // forced flush fails
  assert.deepEqual(col(env, 'MACHINES', 'SR NO'), [2, 10, 4, 3, 1, 8, 7, 5, 6, 9], 'rules still applied');
  const queued = env.queue().length;
  assert.ok(queued >= 3);
  const attempts = env.fetches().length;
  env.edit('MACHINES', 3, 6, 'Completed');     // within backoff: no network call, no exception
  assert.equal(env.fetches().length, attempts);
  assert.ok(env.queue().length > queued);
  env.ctx.__fetchMode = 'ok';
  env.advance(6 * 60);
  env.tick();
  assert.equal(env.queue().length, 0);
  const sent = env.fetches().at(-1).payload;
  assert.equal(sent.sheet_id, CONFIG.sheet_id);
  assert.equal(sent.secret, 's3cret');
  assert.equal(sent.engine_version, env.ctx.ENGINE_VERSION);
  const run = sent.runs.find((r) => r.type === 'run' && r.rule_id === 'sort_by_stage');
  for (const k of ['run_id', 'rule_id', 'trigger_type', 'config_version', 'rows_affected', 'duration_ms', 'status', 'snapshot_ref']) {
    assert.ok(k in run, `run record has ${k}`);
  }
});

test('unknown enum values are reported in the value census', () => {
  const env = engineEnv();
  const runs = env.queue().filter((r) => r.rule_id === 'sort_by_stage');
  assert.deepEqual(runs[0].unknown_values.STATUS.sort(), ['ON HOLD']);
});

test('queue is capped and counts dropped records instead of growing without bound', () => {
  const env = engineEnv();
  for (let i = 0; i < 260; i++) env.edit('MACHINES', 3, 2, `n${i}`);  // non-trigger column: dirty only
  for (let i = 0; i < 120; i++) env.edit('MACHINES', 3, 6, i % 2 ? 'Completed' : 'Cancelled');
  assert.ok(env.queue().length <= 200);
  assert.ok(Number(env.props()['engine.queue.dropped']) > 0);
});

test('config larger than one property is chunked and checksummed', () => {
  const config = clone(CONFIG);
  for (let i = 0; i < 300; i++) config.canonical_headers.push({ canonical: `EXTRA COLUMN ${i} — ünïcödé`, match: [`equals:EXTRA COLUMN ${i} — ünïcödé`] });
  const env = engineEnv(WORKBOOK, config);
  const props = env.props();
  assert.ok(Number(props['engine.config.n']) > 3);
  assert.equal(JSON.parse(env.call('engineStatus')).config_version, 3);
  assert.deepEqual(col(env, 'MACHINES', 'SR NO'), [2, 10, 4, 3, 1, 8, 7, 5, 6, 9]);
  // corrupt one chunk -> engine refuses to run with it, and says so
  env.ctx.__mock.props()['engine.config.1'] = 'garbage';
  env.call('engineSetConfig', 'x');  // invalid JSON does not replace the stored config
  env.edit('MACHINES', 3, 6, 'Cancelled');
  assert.ok(env.queue().some((r) => r.type === 'error' && /checksum mismatch/.test(r.message)));
});

test('config shape errors are structured and nothing is stored', () => {
  const env = rawEnv();
  const bad = JSON.parse(env.call('engineSetConfig', '{"config_version": 1}'));
  assert.equal(bad.ok, false);
  assert.ok(bad.errors.every((e) => typeof e.pointer === 'string' && e.code && e.message));
  const config = clone(CONFIG);
  config.rules[0].action = 'explode';
  const bad2 = JSON.parse(env.call('engineSetConfig', JSON.stringify(config)));
  assert.deepEqual(bad2.errors.map((e) => [e.code, e.pointer]), [['unknown_action', '/rules/0']]);
  assert.equal(env.props()['engine.config.n'], undefined);
});

test('_config mirror is hidden, readable, and never the source of truth', () => {
  const env = engineEnv();
  const mirror = tab(env, '_config');
  assert.equal(mirror.hidden, true);
  assert.match(mirror.cells[0][0].v, /READ-ONLY MIRROR/);
  // tampering with the mirror has no effect
  env.edit('_config', 3, 1, '"config_version": 999,');
  assert.equal(JSON.parse(env.call('engineStatus')).config_version, 3);
});

test('edits on SUMMARY, engine tabs and ungoverned tabs do nothing', () => {
  const env = engineEnv();
  env.mock.resetCalls();
  env.edit('SUMMARY', 3, 3, 'x');
  env.edit('LEGENDS', 3, 1, 'x');
  env.edit('_engine_snapshots', 2, 1, 'x');
  env.advance(200);
  env.tick();
  assert.equal(env.mock.calls.writes, 0);
  assert.equal(env.mock.calls.structural, 0);
});

test('edit while another execution holds the lock is replayed by the next tick', () => {
  const env = engineEnv();
  env.ctx.__lockBusy = true;
  env.edit('MACHINES', 3, 6, 'Cancelled');
  assert.equal(col(env, 'MACHINES', 'SR NO')[0], 2, 'not applied yet');
  assert.deepEqual(JSON.parse(env.props()['engine.pending_tabs']), ['MACHINES']);
  env.ctx.__lockBusy = false;
  env.tick();
  assert.notEqual(col(env, 'MACHINES', 'SR NO')[0], 2);
  assert.equal(env.props()['engine.pending_tabs'], undefined);
});

test('writes are batched range ops (no per-cell loops)', () => {
  function bigWorkbook(n) {
    const wb = clone(WORKBOOK);
    const m = wb.tabs.find((t) => t.name === 'MACHINES');
    const base = m.rows.slice(2);
    m.rows = m.rows.slice(0, 2);
    for (let i = 0; i < n; i++) {
      const r = clone(base[(i * 7) % base.length]);
      r[0] = i + 1;
      m.rows.push(r);
    }
    m.number_formats = [];
    return wb;
  }
  const counts = [20, 600].map((n) => {
    const env = engineEnv(bigWorkbook(n), { ...CONFIG, guards: { ...CONFIG.guards, max_rows_per_run: 10000 } }, { run: false });
    env.mock.resetCalls();
    env.call('engineRunAll');
    return env.mock.calls;
  });
  const [small, big] = counts;
  assert.ok(big.writes < 60, `600-row run used ${big.writes} write calls`);
  assert.ok(big.reads < 40, `600-row run used ${big.reads} read calls`);
  assert.ok(big.writes <= small.writes + 20, `writes scale with rows: ${small.writes} -> ${big.writes}`);
});

test('engineInstall replaces its own and orphaned triggers; menu is installable, not simple', () => {
  const env = rawEnv();
  env.ctx.ScriptApp.newTrigger('onEditAutoSort').forSpreadsheet(env.mock.ss).onEdit().create();
  env.call('engineInstall');
  env.call('engineInstall');
  const handlers = Array.from(env.mock.triggers(), (t) => t.fn).sort();
  assert.deepEqual(handlers, ['engineOnEdit', 'engineOnOpen', 'engineTick']);
  assert.equal(typeof env.ctx.onEdit, 'undefined', 'no simple onEdit');
  assert.equal(typeof env.ctx.onOpen, 'undefined', 'no simple onOpen');
  env.open();
  assert.equal(env.mock.ss.menus[0].title, '⚙️ Automation');
});

test('engineHeaderHashes matches the pre-flight hashes in the reference config', () => {
  const env = rawEnv();
  const hashes = JSON.parse(env.call('engineHeaderHashes', 2));
  assert.equal(hashes.MACHINES, CONFIG.schema_hashes.MACHINES);
  assert.equal(hashes.SPARES, CONFIG.schema_hashes.SPARES);
  assert.ok('LEGENDS' in hashes);
});

test('menu config install path', () => {
  const env = rawEnv();
  env.mock.ui._prompts.push(JSON.stringify(CONFIG));
  env.call('menuInstallConfig');
  assert.equal(JSON.parse(env.call('engineStatus')).config_version, 3);
});

test('engine source has no per-sheet constants and no silent catch blocks', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'Engine.gs'), 'utf8');
  for (const word of ['MACHINES', 'SPARES', 'LEGENDS', "'DISPATCH", "'FREEZE", "'STATUS'", 'PROCESS']) {
    assert.ok(!src.includes(word), `Engine.gs hard-codes ${word}`);
  }
  const catches = [...src.matchAll(/catch\s*\((\w+)\)\s*\{([^}]*)\}/g)];
  assert.ok(catches.length > 0);
  for (const [, name, body] of catches) {
    const handled = body.includes(name) || /throw|ruleAbort/.test(body);
    assert.ok(handled, `catch (${name}) swallows the error: ${body.trim()}`);
  }
});
