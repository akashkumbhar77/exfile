// ---------------------------------------------------------------- shared helpers
// These are the same in every generated script. They mirror the server evaluator's
// value semantics exactly; the server is the reference (PATCH-004 D.6).

/** Text form of a cell (matches the server's String() rules). */
function cellText_(value) {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return dateText_(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (isFinite(value) && Math.floor(value) === value) return String(Math.round(value));
    return String(value);
  }
  return String(value);
}

function dateText_(d) {
  var pad = function (n) { return (n < 10 ? '0' : '') + n; };
  var ymd = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  if (!d.getHours() && !d.getMinutes() && !d.getSeconds() && !d.getMilliseconds()) return ymd;
  return ymd + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

/** How rules compare text: trimmed and upper-cased. */
function normText_(value) { return cellText_(value).trim().toUpperCase(); }

/** Only '' and blank cells count as empty; whitespace is content. */
function isEmpty_(value) { return value === null || value === undefined || value === ''; }

/** Real date cells only; anything else is "not a date" (date conditions, auto sort). */
function dateCellMs_(value) { return value instanceof Date ? value.getTime() : null; }

/** Date cells, plus YYYY-MM-DD text, for sort keys declared as dates. */
function sortDateMs_(value) {
  if (value instanceof Date) return value.getTime();
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.trim())) return null;
  var p = value.trim().split('-');
  var y = Number(p[0]), m = Number(p[1]), day = Number(p[2]);
  var d = new Date(y, m - 1, day);
  // a real date only: 2026-02-30 rolls over into March, so reject it
  if (d.getFullYear() !== y || d.getMonth() !== m - 1 || d.getDate() !== day) return null;
  return d.getTime();
}

/** Number cells, plus plain numeric text, for sort keys declared as numbers. */
function sortNumber_(value) {
  if (typeof value === 'boolean') return null;
  if (typeof value === 'number') return isFinite(value) ? value : null;
  var text = cellText_(value).trim();
  return /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(text) ? Number(text) : null;
}

/** Numbers for numeric conditions: real number cells only, never numeric-looking text. */
function numberCell_(value) {
  return (typeof value === 'number' && isFinite(value)) ? value : null;
}

function compareValues_(a, b) { return a > b ? 1 : (a < b ? -1 : 0); }

/** One sort key: blanks (null) go last or first, everything else compares normally. */
function compareKey_(a, b, direction, blanksLast) {
  if (a === null && b === null) return 0;
  if (a === null) return blanksLast;
  if (b === null) return -blanksLast;
  return direction * compareValues_(a, b);
}

/** Auto sort key: dates, then numbers, then booleans, then text - each group together. */
function autoKey_(value) {
  if (isEmpty_(value)) return null;
  if (value instanceof Date) return [0, value.getTime()];
  if (typeof value === 'boolean') return [2, value ? 1 : 0];
  if (typeof value === 'number') return [1, value];
  return [3, cellText_(value).toLowerCase()];
}

function compareAuto_(a, b, direction, blanksLast) {
  if (a === null && b === null) return 0;
  if (a === null) return blanksLast;
  if (b === null) return -blanksLast;
  var d = compareValues_(a[0], b[0]);
  return direction * (d ? d : compareValues_(a[1], b[1]));
}

/** The `!hold` marker (invariant 12): a ticked box, a non-zero number, a date, or any text
 *  except FALSE / NO / 0. Held rows are exempt from every rule. */
function holdIsSet_(value) {
  if (value === null || value === undefined || value === false) return false;
  if (value === true) return true;
  if (typeof value === 'number') return value !== 0;
  if (value instanceof Date) return true;
  var t = normText_(value);
  return t !== '' && t !== 'FALSE' && t !== 'NO' && t !== '0';
}

/** Columns are found by header text, never by position.
 *  `raw` skips canonical matching, for a tab this script generated (its headers are already
 *  canonical, and re-matching them could rename a column). */
function buildView_(sheet, raw) {
  var lastCol = sheet.getLastColumn();
  var headers = lastCol ? sheet.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0] : [];
  var cols = {}, holdIdx = -1, order = [];
  for (var i = 0; i < headers.length; i++) {
    if (cellText_(headers[i]).trim() === '') continue;
    if (normText_(headers[i]) === HOLD_COLUMN) { if (holdIdx < 0) holdIdx = i; continue; }
    var name = raw ? cellText_(headers[i]).trim() : canonicalHeader_(headers[i]).trim();
    var key = name.toUpperCase();
    order.push({ at: i, key: key, name: name });   // every column, in sheet order
    if (!(key in cols)) cols[key] = i;   // first matching column wins
  }
  var lastRow = sheet.getLastRow();
  var count = Math.max(0, lastRow - DATA_START_ROW + 1);
  return {
    sheet: sheet, name: sheet.getName(), cols: cols, order: order, holdIdx: holdIdx, lastCol: lastCol,
    headers: headers, rowCount: count,
    values: count ? sheet.getRange(DATA_START_ROW, 1, count, lastCol).getValues() : []
  };
}

/** The data rows' colours and strike-through, read once per run and then kept in step. */
function viewFormats_(view) {
  if (!view.formats) {
    var range = view.sheet.getRange(DATA_START_ROW, 1, view.rowCount, view.lastCol);
    view.formats = { range: range, fonts: range.getFontColors(), fills: range.getBackgrounds(),
                     lines: range.getFontLines() };
  }
  return view.formats;
}

function writeValues_(view, values) {
  return function () {
    view.sheet.getRange(DATA_START_ROW, 1, values.length, view.lastCol).setValues(values);
  };
}

function writeFormats_(plan) {
  return function () {
    plan.range.setFontColors(plan.fonts);
    plan.range.setBackgrounds(plan.fills);
    plan.range.setFontLines(plan.lines);
  };
}

function isHeld_(view, row) {
  return view.holdIdx >= 0 && holdIsSet_(row[view.holdIdx]);
}

/** Header fingerprint: trimmed header cells, trailing blanks dropped, joined with U+001F,
 *  SHA-256, lower-case hex. Identical to the server's pre-flight hash. */
function headerHash_(view) {
  var cells = [];
  for (var i = 0; i < view.headers.length; i++) cells.push(cellText_(view.headers[i]).trim());
  while (cells.length && cells[cells.length - 1] === '') cells.pop();
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,
                                      cells.join('\u001F'), Utilities.Charset.UTF_8);
  var hex = '';
  for (var b = 0; b < bytes.length; b++) {
    var part = (bytes[b] < 0 ? bytes[b] + 256 : bytes[b]).toString(16);
    hex += part.length === 1 ? '0' + part : part;
  }
  return hex;
}

/** Midnight today in the spreadsheet's time zone, so "before today" means what the owner sees. */
function todayStart_(spreadsheet) {
  var tz = spreadsheet.getSpreadsheetTimeZone();
  var parts = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd').split('-');
  return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])).getTime();
}

function dayAfter_(ms) { var d = new Date(ms); d.setDate(d.getDate() + 1); return d.getTime(); }

/** Text sort key: blank stays blank, everything else compares case-insensitively. */
function sortText_(value) { return isEmpty_(value) ? null : cellText_(value).toLowerCase(); }

function sameRow_(a, b) {
  if (a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** Apply one style value across a whole row's worth of cells. */
function fillRow_(cells, value) { for (var i = 0; i < cells.length; i++) cells[i] = value; }

// ---- date conditions -------------------------------------------------------
function dateBefore_(value, ms) { var t = dateCellMs_(value); return t !== null && t < ms; }
function dateAfter_(value, ms) { var t = dateCellMs_(value); return t !== null && t >= dayAfter_(ms); }

// ---- numeric conditions ----------------------------------------------------
// Every operand is a real number cell or null; null anywhere makes the comparison false, so
// blanks, numeric-looking text and division by zero are safe non-matches.
function numAdd_(a, b) { return (a === null || b === null) ? null : finite_(a + b); }
function numSub_(a, b) { return (a === null || b === null) ? null : finite_(a - b); }
function numMul_(a, b) { return (a === null || b === null) ? null : finite_(a * b); }
function numDiv_(a, b) { return (a === null || b === null || b === 0) ? null : finite_(a / b); }
function numAbs_(a) { return a === null ? null : Math.abs(a); }
function finite_(x) { return isFinite(x) ? x : null; }

/** Rounds half away from zero on the displayed decimal, like the sheet's ROUND (2.5 -> 3,
 *  2.675 -> 2.68), not like the binary-float rounding most languages do. */
function numRound_(value, digits) {
  if (value === null) return null;
  var s = String(value);
  if (s.indexOf('e') >= 0 || s.indexOf('E') >= 0) return Number(value.toFixed(digits));
  var negative = s.charAt(0) === '-';
  if (negative) s = s.substring(1);
  var dot = s.indexOf('.');
  if (dot < 0) return value;
  var frac = s.substring(dot + 1);
  if (frac.length <= digits) return value;
  var kept = Number(s.substring(0, dot) + frac.substring(0, digits));
  var rounded = (kept + (frac.charAt(digits) >= '5' ? 1 : 0)) / Math.pow(10, digits);
  return negative ? -rounded : rounded;
}

function numEq_(a, b) { return a !== null && b !== null && a === b; }
function numNe_(a, b) { return a !== null && b !== null && a !== b; }
function numGt_(a, b) { return a !== null && b !== null && a > b; }
function numGte_(a, b) { return a !== null && b !== null && a >= b; }
function numLt_(a, b) { return a !== null && b !== null && a < b; }
function numLte_(a, b) { return a !== null && b !== null && a <= b; }
function numBetween_(a, lo, hi) { return a !== null && lo !== null && hi !== null && a >= lo && a <= hi; }
function numNotBetween_(a, lo, hi) {
  return a !== null && lo !== null && hi !== null && !(a >= lo && a <= hi);
}

/** True when every cell in the row is blank. */
function rowIsEmpty_(row) {
  for (var i = 0; i < row.length; i++) if (!isEmpty_(row[i])) return false;
  return true;
}

/** The target tab of a consolidate rule, created in first position if this is the first run
 *  (where the server engine puts it, so both targets end up with the same tab order). */
function targetSheet_(ss, name) {
  var sheet = ss.getSheetByName(name);
  return sheet ? sheet : ss.insertSheet(name, 0);
}

/** Locks the tab against hand edits: it is rebuilt from its sources every run, so an edit
 *  made here would be silently overwritten. The rest of the spreadsheet stays editable. */
function lockTab_(sheet) {
  var existing = sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET);
  if (!existing.length) sheet.protect().setDescription(PRODUCT + ': rebuilt automatically');
}

/** An array of `n` copies of one value, for the batched style setters. */
function fillArray_(n, value) {
  var out = [];
  for (var i = 0; i < n; i++) out.push(value);
  return out;
}
