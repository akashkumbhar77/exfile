'use strict';
/**
 * Loads an Apps Script file (Engine.gs or legacy.gs) into an isolated V8 context
 * together with gas_mock.js, with a controllable clock.
 *
 * Library:  const { createEnv } = require('./harness');
 * CLI:      node harness.js < request.json > response.json   (used by backend pytest)
 *
 * Cell encoding across the JSON boundary: dates are {"$date": "YYYY-MM-DD"} or
 * {"$datetime": "YYYY-MM-DDTHH:MM:SS"} in local (TZ) time; everything else is plain JSON.
 */
process.env.TZ = process.env.TZ || 'Asia/Kolkata';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const zlib = require('node:zlib');

const ENGINE_DIR = path.resolve(__dirname, '..');
const SCRIPTS = {
  engine: path.join(ENGINE_DIR, 'Engine.gs'),
  legacy: path.join(ENGINE_DIR, 'reference', 'legacy.gs'),
};
const MOCK = fs.readFileSync(path.join(__dirname, 'gas_mock.js'), 'utf8');

const pad = (n, w = 2) => String(n).padStart(w, '0');

function formatDate(ms, fmt) {
  const d = new Date(ms);
  const parts = {
    yyyy: pad(d.getFullYear(), 4), MM: pad(d.getMonth() + 1), dd: pad(d.getDate()),
    HH: pad(d.getHours()), mm: pad(d.getMinutes()), ss: pad(d.getSeconds()),
  };
  let out = '';
  for (let i = 0; i < fmt.length;) {
    if (fmt[i] === "'") {
      const end = fmt.indexOf("'", i + 1);
      out += fmt.slice(i + 1, end);
      i = end + 1;
      continue;
    }
    const tok = ['yyyy', 'MM', 'dd', 'HH', 'mm', 'ss'].find((t) => fmt.startsWith(t, i));
    if (tok) { out += parts[tok]; i += tok.length; } else { out += fmt[i]; i += 1; }
  }
  return out;
}

const toBuf = (bytes) => Buffer.from(Array.from(bytes, (x) => (x < 0 ? x + 256 : x)));

const PRELUDE = `
(function (g) {
  const RealDate = Date;
  function FakeDate(...args) {
    if (!new.target) return new RealDate(g.__now).toString();
    return args.length ? new RealDate(...args) : new RealDate(g.__now);
  }
  FakeDate.prototype = RealDate.prototype;
  FakeDate.now = () => g.__now;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.parse = RealDate.parse;
  g.Date = FakeDate;

  g.__decode = function (v) {
    if (v && typeof v === 'object') {
      if ('$date' in v) { const [y, m, d] = v.$date.split('-').map(Number); return new RealDate(y, m - 1, d); }
      if ('$datetime' in v) {
        const [dp, tp] = v.$datetime.split('T');
        const [y, m, d] = dp.split('-').map(Number);
        const [hh, mi, ss] = tp.split(':').map(Number);
        return new RealDate(y, m - 1, d, hh, mi, ss || 0);
      }
      throw new Error('cannot decode ' + JSON.stringify(v));
    }
    return v;
  };
  g.__encode = function (v) {
    if (Object.prototype.toString.call(v) !== '[object Date]') return v;
    const p = (n, w) => String(n).padStart(w || 2, '0');
    const day = p(v.getFullYear(), 4) + '-' + p(v.getMonth() + 1) + '-' + p(v.getDate());
    if (!v.getHours() && !v.getMinutes() && !v.getSeconds() && !v.getMilliseconds()) return { $date: day };
    return { $datetime: day + 'T' + p(v.getHours()) + ':' + p(v.getMinutes()) + ':' + p(v.getSeconds()) };
  };
})(globalThis);
`;

function createEnv({ script = 'engine', now = '2026-09-16T10:00:00', tz = process.env.TZ, scriptSource } = {}) {
  const logs = [];
  const sandboxConsole = {
    log: (...a) => logs.push(['log', a.map(String).join(' ')]),
    info: (...a) => logs.push(['info', a.map(String).join(' ')]),
    warn: (...a) => logs.push(['warn', a.map(String).join(' ')]),
    error: (...a) => logs.push(['error', a.map(String).join(' ')]),
  };
  const ctx = vm.createContext({ console: sandboxConsole });
  ctx.__tz = tz;
  ctx.__now = new Date(now).getTime();
  ctx.__node = {
    sha256: (t) => crypto.createHash('sha256').update(String(t), 'utf8').digest(),
    uuid: () => crypto.randomUUID(),
    byteLength: (s) => Buffer.byteLength(s, 'utf8'),
    stringToBuf: (s) => Buffer.from(s, 'utf8'),
    bytesToBuf: toBuf,
    bufToString: (b) => Buffer.from(b).toString('utf8'),
    gzip: (b) => zlib.gzipSync(b),
    gunzip: (b) => zlib.gunzipSync(b),
    base64Encode: (bytes) => toBuf(bytes).toString('base64'),
    base64Decode: (t) => Buffer.from(t, 'base64'),
    formatDate,
  };
  vm.runInContext(PRELUDE, ctx, { filename: 'prelude.js' });
  vm.runInContext(MOCK, ctx, { filename: 'gas_mock.js' });
  const file = SCRIPTS[script];
  const source = scriptSource || fs.readFileSync(file, 'utf8');
  const compiled = new vm.Script(source, { filename: path.basename(file || 'script.gs') });
  // Apps Script evaluates the whole project afresh for every execution, so module-level
  // state (caches) never survives between triggers. Re-run the script before each entry.
  const fresh = () => compiled.runInContext(ctx);
  fresh();

  const env = {
    ctx,
    logs,
    mock: ctx.__mock,
    call: (name, ...args) => {
      fresh();
      if (typeof ctx[name] !== 'function') throw new Error(`no function ${name} in ${script}`);
      return ctx[name](...args);
    },
    load: (workbook) => ctx.__mock.loadWorkbook(workbook),
    setNow: (iso) => { ctx.__now = new Date(iso).getTime(); },
    advance: (seconds) => { ctx.__now += seconds * 1000; },
    edit: (tab, row, col, value) => { fresh(); return ctx.__mock.edit(tab, row, col, value); },
    tick: () => { fresh(); return ctx.__mock.fire('CLOCK', {}); },
    open: () => { fresh(); return ctx.__mock.fire('ON_OPEN', {}); },
    // JSON round-trip strips vm-realm prototypes so node:assert deepStrictEqual works.
    dump: (opts) => JSON.parse(JSON.stringify(ctx.__mock.dump(opts))),
    props: () => JSON.parse(JSON.stringify(ctx.__mock.props())),
    queue: () => {
      const p = ctx.__mock.props();
      const n = Number(p['engine.queue.n'] || 0);
      let text = '';
      for (let i = 0; i < n; i++) text += p[`engine.queue.${i}`];
      return text ? JSON.parse(text) : [];
    },
    fetches: () => JSON.parse(JSON.stringify(ctx.__mock.fetches)),
    toasts: () => JSON.parse(JSON.stringify(ctx.__mock.ss.toasts)),
  };
  return env;
}

/** Tabs a user would see (engine bookkeeping tabs removed), keyed for comparison. */
function visibleTabs(dump) {
  return dump.filter((t) => t.name !== '_config' && t.name !== '_engine_snapshots');
}

function runRequest(req) {
  const env = createEnv({ script: req.script, now: req.now, scriptSource: req.scriptSource });
  env.load(req.workbook);
  const results = [];
  for (const step of req.steps || []) {
    if (step.call) {
      const args = (step.args || []).map((a) => (a && typeof a === 'object' && '$json' in a ? JSON.stringify(a.$json) : a));
      results.push(env.call(step.call, ...args));
    } else if (step.edit) {
      results.push(env.edit(...step.edit));
    } else if (step.advance) {
      env.advance(step.advance);
    } else if (step.tick) {
      results.push(env.tick());
    } else {
      throw new Error('unknown step ' + JSON.stringify(step));
    }
  }
  return {
    results,
    tabs: env.dump({ minRows: req.dump_min_rows || 0, minCols: req.dump_min_cols || 0 }),
    queue: env.queue(),
    props: env.props(),
    fetches: env.fetches(),
    calls: JSON.parse(JSON.stringify(env.mock.calls)),
    toasts: env.toasts(),
    logs: env.logs,
  };
}

module.exports = { createEnv, visibleTabs, runRequest, formatDate };

if (require.main === module) {
  const input = fs.readFileSync(0, 'utf8');
  let out;
  try {
    const req = JSON.parse(input);
    out = Array.isArray(req) ? req.map(runRequest) : runRequest(req);
  } catch (err) {
    process.stderr.write(String(err && err.stack || err));
    process.exit(1);
  }
  process.stdout.write(JSON.stringify(out));
}
