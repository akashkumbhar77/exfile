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
  var origin = [];
  for (var r = 0; r < count; r++) origin.push(r);
  return {
    sheet: sheet, name: sheet.getName(), cols: cols, order: order, holdIdx: holdIdx, lastCol: lastCol,
    headers: headers, rowCount: count,
    values: count ? sheet.getRange(DATA_START_ROW, 1, count, lastCol).getValues() : [],
    // Where each data row stood when the run started (-1: a row this run inserted), so the
    // cell formatting read from the sheet follows its row, as it will once the run is written.
    origin: origin, baseCount: count,
    // How far down the data went when the run started: formatting still covers rows that
    // earlier rules in this run emptied (but not rows they removed).
    extent: count
  };
}

/**
 * The data rows' colours and strike-through as earlier rules in this run left them: read from
 * the sheet once, then moved with their rows. Rows this run inserted have none.
 */
function viewFormats_(view) {
  if (!view.formats) {
    var read = { fonts: [], fills: [], lines: [] };
    if (view.baseCount) {
      var range = view.sheet.getRange(DATA_START_ROW, 1, view.baseCount, view.lastCol);
      read = { fonts: range.getFontColors(), fills: range.getBackgrounds(), lines: range.getFontLines() };
    }
    var fonts = [], fills = [], lines = [];
    for (var i = 0; i < view.origin.length; i++) {
      var o = view.origin[i];
      fonts.push(o >= 0 ? read.fonts[o] : fillArray_(view.lastCol, DEFAULT_FONT));
      fills.push(o >= 0 ? read.fills[o] : fillArray_(view.lastCol, DEFAULT_FILL));
      lines.push(o >= 0 ? read.lines[o] : fillArray_(view.lastCol, 'none'));
    }
    view.formats = { fonts: fonts, fills: fills, lines: lines };
  }
  return view.formats;
}

var DEFAULT_FONT = '#000000';   // what Sheets reports for a cell nobody coloured
var DEFAULT_FILL = '#ffffff';

function writeFormats_(view, plan) {
  return function () {
    var range = view.sheet.getRange(DATA_START_ROW, 1, plan.fonts.length, view.lastCol);
    range.setFontColors(plan.fonts);
    range.setBackgrounds(plan.fills);
    range.setFontLines(plan.lines);
  };
}

/** Dropdowns on the given data rows of one column, one range per run of consecutive rows. */
function writeDropdowns_(view, col, rows, values, allowInvalid) {
  return function () {
    var rule = SpreadsheetApp.newDataValidation().requireValueInList(values, true)
        .setAllowInvalid(allowInvalid).build();
    for (var s = 0; s < rows.length;) {
      var e = s;
      while (e + 1 < rows.length && rows[e + 1] === rows[e] + 1) e++;
      view.sheet.getRange(DATA_START_ROW + rows[s], col + 1, e - s + 1, 1).setDataValidation(rule);
      s = e + 1;
    }
  };
}

/** The last data row (0-based) holding anything, or -1: where "the bottom" of the data is. */
function lastContentIndex_(view) {
  for (var i = view.values.length - 1; i >= 0; i--) if (!rowIsEmpty_(view.values[i])) return i;
  return -1;
}

/**
 * Inserts rows so the first lands at data index `at`; everything from there down moves down, and
 * the new rows start with no formatting and no dropdowns. Planned now, written at the end.
 */
function insertDataRows_(run, view, at, rows) {
  var blank = [];
  for (var b = 0; b < rows.length; b++) blank.push(-1);
  Array.prototype.splice.apply(view.values, [at, 0].concat(rows));
  Array.prototype.splice.apply(view.origin, [at, 0].concat(blank));
  if (view.formats) {
    for (var i = 0; i < rows.length; i++) {
      view.formats.fonts.splice(at + i, 0, fillArray_(view.lastCol, DEFAULT_FONT));
      view.formats.fills.splice(at + i, 0, fillArray_(view.lastCol, DEFAULT_FILL));
      view.formats.lines.splice(at + i, 0, fillArray_(view.lastCol, 'none'));
    }
  }
  view.rowCount = view.values.length;
  run.writes.push(function () {
    var sheet = view.sheet, first = DATA_START_ROW + at;
    if (first > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), rows.length);
    else sheet.insertRowsBefore(first, rows.length);
    var range = sheet.getRange(first, 1, rows.length, view.lastCol);
    range.clearFormat();
    range.clearDataValidations();
    range.setValues(rows);
  });
}

/** Removes data rows (0-based, ascending); everything below moves up. Planned now, written at the end. */
function deleteDataRows_(run, view, rows) {
  for (var d = rows.length - 1; d >= 0; d--) {
    view.values.splice(rows[d], 1);
    view.origin.splice(rows[d], 1);
    if (view.formats) {
      view.formats.fonts.splice(rows[d], 1);
      view.formats.fills.splice(rows[d], 1);
      view.formats.lines.splice(rows[d], 1);
    }
  }
  view.rowCount = view.values.length;
  var positions = rows.slice();
  run.writes.push(function () {
    for (var e = positions.length - 1; e >= 0;) {   // bottom up, one call per block of rows
      var s = e;
      while (s > 0 && positions[s - 1] === positions[s] - 1) s--;
      view.sheet.deleteRows(DATA_START_ROW + positions[s], e - s + 1);
      e = s - 1;
    }
  });
}

/** Writes only the given data rows (0-based, ascending), a block of consecutive rows at a time,
 *  so the other rows - and any formulas in them - are not touched. */
function writeRows_(view, rows) {
  var values = view.values.slice();   // as planned now: later rules may move rows in the live picture
  return function () {
    for (var s = 0; s < rows.length;) {
      var e = s;
      while (e + 1 < rows.length && rows[e + 1] === rows[e] + 1) e++;
      view.sheet.getRange(DATA_START_ROW + rows[s], 1, e - s + 1, view.lastCol)
          .setValues(values.slice(rows[s], rows[e] + 1));
      s = e + 1;
    }
  };
}

/** Empties the given cells (data row, column; both 0-based) in one call, leaving every other cell -
 *  and any formula in it - as it is. */
function clearCells_(view, cells) {
  var a1 = [];
  for (var i = 0; i < cells.length; i++) a1.push(columnLetter_(cells[i][1] + 1) + (DATA_START_ROW + cells[i][0]));
  return function () {
    view.sheet.getRangeList(a1).clearContent();
  };
}

function columnLetter_(n) {
  var out = '';
  for (; n > 0; n = Math.floor((n - 1) / 26)) out = String.fromCharCode(65 + (n - 1) % 26) + out;
  return out;
}

// ---------------------------------------------------------------- the backup tab

/*
 * Before a rule removes or overwrites rows, they are copied to a hidden tab, one row each:
 * when the run started, the rule, the tab, the row number, then the row's cells as they were.
 * Only the last BACKUP_RUNS runs are kept (see the settings at the top). It is not a snapshot: colours, dropdowns and formulas
 * are not kept, and nothing puts rows back automatically - copy them back by hand if you need to.
 */
var BACKUP_TAB = '_backup';
var BACKUP_HEADER = ['BACKED UP', 'RULE', 'TAB', 'ROW', 'ROW AS IT WAS'];

function backUp_(run, rule, view, i) {
  run.backup.push([rule, view.name, DATA_START_ROW + i].concat(view.values[i]));
}

function writeBackup_(ss, entries) {
  var stamp = new Date(Math.floor(Date.now() / 1000) * 1000);   // this run, to the second
  var sheet = ss.getSheetByName(BACKUP_TAB);
  if (!sheet) sheet = ss.insertSheet(BACKUP_TAB, ss.getSheets().length);
  var rows = [], last = sheet.getLastRow(), width = sheet.getLastColumn();
  if (last >= 2 && width) {
    var kept = sheet.getRange(2, 1, last - 1, width).getValues();
    for (var k = 0; k < kept.length; k++) if (!rowIsEmpty_(kept[k])) rows.push(kept[k]);
  }
  for (var e = 0; e < entries.length; e++) rows.push([stamp].concat(entries[e]));

  var runs = [], seen = {};
  for (var r = 0; r < rows.length; r++) {
    var key = stampKey_(rows[r][0]);
    if (!seen[key]) { seen[key] = true; runs.push(key); }
  }
  var keep = {};
  for (var q = Math.max(0, runs.length - BACKUP_RUNS); q < runs.length; q++) keep[runs[q]] = true;
  var out = [BACKUP_HEADER.slice()], cols = BACKUP_HEADER.length;
  for (var o = 0; o < rows.length; o++) {
    if (!keep[stampKey_(rows[o][0])]) continue;
    out.push(rows[o]);
    cols = Math.max(cols, rows[o].length);
  }
  for (var p = 0; p < out.length; p++) while (out[p].length < cols) out[p].push('');
  sheet.clearContents();
  if (sheet.getMaxColumns() < cols) sheet.insertColumnsAfter(sheet.getMaxColumns(), cols - sheet.getMaxColumns());
  if (sheet.getMaxRows() < out.length) sheet.insertRowsAfter(sheet.getMaxRows(), out.length - sheet.getMaxRows());
  sheet.getRange(1, 1, out.length, cols).setValues(out);
  sheet.hideSheet();
}

function stampKey_(value) {
  return value instanceof Date ? 'd' + value.getTime() : 's' + String(value);
}

/** Rows as a list of lookup keys: null when a key column is missing or every key cell is empty. */
function keyOf_(row, view, keys) {
  var parts = [], any = false;
  for (var k = 0; k < keys.length; k++) {
    var at = view.cols[keys[k]];
    if (at === undefined) return null;
    var part = normText_(row[at]);
    if (part !== '') any = true;
    parts.push(part);
  }
  return any ? JSON.stringify(parts) : null;
}

/** A row laid out in another tab's columns, matched by header name. Never drops a value. */
function toTarget_(row, from, to) {
  var out = fillArray_(to.lastCol, '');
  for (var o = 0; o < from.order.length; o++) {
    var value = row[from.order[o].at];
    if (isEmpty_(value)) continue;
    var at = to.cols[from.order[o].key];
    if (at === undefined) {
      throw new Error('tab ' + from.name + ' column ' + from.order[o].name + ' has no match in ' +
                      to.name + ' - refusing to drop data');
    }
    if (isEmpty_(out[at])) out[at] = value;
  }
  return out;
}

function viewNamed_(run, name) {
  for (var v = 0; v < run.views.length; v++) if (run.views[v].name === name) return run.views[v];
  return null;
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
