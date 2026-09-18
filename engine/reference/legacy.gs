/**********************************************************************
 *  GOOGLE SHEETS AUTO-SORT  (status-stage version)
 *
 *  Sorts EVERY tab automatically on edit:
 *
 *   LEVEL 1 — STATUS stage order:
 *       1. IN-PROCESS
 *       2. COMPLETED
 *       3. DISPUTED
 *       4. DISPATCHED
 *       5. CANCELLED
 *      (anything else / blank goes to the bottom)
 *
 *   LEVEL 2 — within EVERY status group, by TENTATIVE DISPATCH DATE,
 *      simple ascending (earliest first; blank dates last).
 *
 *   FONT COLOR by status (whole row):
 *       IN-PROCESS = red, COMPLETED = green, DISPUTED = yellow,
 *       DISPATCHED = black, CANCELLED = black + strikethrough.
 *       (overdue In-Process rows use black text for readability)
 *
 *   FREEZE? = YES -> the word "YES" is shown in green text (no cell fill).
 *
 *   ROW BACKGROUND:
 *       Overdue In-Process (dispatch date past today) = light pink ;
 *       otherwise cleared so the alternating banding shows through.
 *
 *   SUMMARY tab: locked, auto-generated consolidation of ALL rows from
 *       every category tab (SOURCE SHEET column added), sorted by status
 *       stage then dispatch date across all sheets.
 *
 *  Columns are detected from the header row (row 2), so it works
 *  across all tabs regardless of column position. The old PRIORITY
 *  column is no longer used and can be deleted from the sheet.
 *
 *  SETUP:
 *   1. Extensions > Apps Script
 *   2. Delete existing code, paste ALL of this, click Save
 *   3. (Already installed earlier — no need to re-run installTrigger)
 **********************************************************************/

var HEADER_ROW = 2;        // Row 2 holds the column titles
var DATA_START_ROW = 3;    // Data begins on row 3

// Font color per status stage (edit hex codes to taste).
// strike:true draws strikethrough text.
var STAGE_FORMAT = {
  1: { color: '#FF0000', strike: false },  // IN-PROCESS  -> red
  2: { color: '#008000', strike: false },  // COMPLETED   -> green
  3: { color: '#D4A017', strike: false },  // DISPUTED    -> yellow (readable gold on white)
  4: { color: '#000000', strike: false },  // DISPATCHED  -> black (no strikethrough)
  5: { color: '#000000', strike: true  },  // CANCELLED   -> black + strikethrough
  6: { color: '#000000', strike: false }   // unknown/blank
};

// Row background when WORK ORDER FREEZE? (or TECHNICAL FREEZE?) = YES.
// NO / blank rows are cleared so the sheet's alternating banding shows through.
var FREEZE_YES_TEXT = '#008000'; // green text for the word YES in the freeze cell (text only, no fill)

// Background for OVERDUE in-process rows (dispatch date already past, status In-Process).
var OVERDUE_BG = '#FCE4EC';      // light pink
// Font used on overdue rows so the text is readable on the red background
// (avoids unreadable red-on-red for In-Process).
var OVERDUE_FONT = '#000000';    // black

// Name of the auto-generated, locked dashboard tab.
var SUMMARY_SHEET = 'SUMMARY';

/**
 * Map a STATUS cell to its stage number (smaller = higher up).
 * Matches by keyword so prefixes like "A. IN PROCESS" still work.
 */
function statusStage(value) {
  var u = String(value).trim().toUpperCase();
  if (u.indexOf('PROCESS') !== -1) return 1;   // IN-PROCESS
  if (u.indexOf('COMPLET') !== -1) return 2;   // COMPLETED
  if (u.indexOf('DISPUT') !== -1) return 3;    // DISPUTED  (sorts before dispatched)
  if (u.indexOf('DISPATCH') !== -1) return 4;  // DISPATCHED
  if (u.indexOf('CANCEL') !== -1) return 5;    // CANCELLED
  return 6;                                    // unknown / blank -> bottom
}

/**
 * Dispatch date as milliseconds for chronological (ascending) sorting.
 * Blank / invalid dates return Infinity so they sit at the bottom of their group.
 */
function dateMs(value) {
  var t = (value instanceof Date) ? value.getTime()
        : (value === '' || value === null || value === undefined) ? NaN
        : new Date(value).getTime();
  return isNaN(t) ? Infinity : t;
}

/**
 * Sorts one sheet. Detects STATUS and DISPATCH columns from row 2.
 */
function sortSheet(sheet) {
  var lastCol = sheet.getLastColumn();
  var lastRow = sheet.getLastRow();
  if (lastCol < 1 || lastRow <= DATA_START_ROW) return;

  var headers = sheet.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0];
  var statusCol = 0, dispatchCol = 0, freezeCol = 0;
  for (var c = 0; c < headers.length; c++) {
    var h = String(headers[c]).trim().toUpperCase();
    if (h === 'STATUS') statusCol = c + 1;
    if (h.indexOf('DISPATCH') !== -1) dispatchCol = c + 1;  // "DISPATCH DATE" / "TENTATIVE DISPATCH DATE"
    if (h.indexOf('FREEZE') !== -1) freezeCol = c + 1;      // "WORK ORDER FREEZE?" / "TECHNICAL FREEZE?"
  }
  if (statusCol === 0) return;  // no STATUS column (e.g. LEGENDS) -> skip

  var today = new Date();
  today.setHours(0, 0, 0, 0);
  var todayMs = today.getTime();

  var numRows = lastRow - DATA_START_ROW + 1;
  var range = sheet.getRange(DATA_START_ROW, 1, numRows, lastCol);
  var data = range.getValues();

  // Tag each row with its original index to keep sorting stable on exact ties
  data.forEach(function (row, i) { row.__i = i; });

  data.sort(function (a, b) {
    var sa = statusStage(a[statusCol - 1]);
    var sb = statusStage(b[statusCol - 1]);
    if (sa !== sb) return sa - sb;

    // Within every status group: dispatch date ascending (earliest first, blanks last)
    if (dispatchCol > 0) {
      var da = dateMs(a[dispatchCol - 1]);
      var db = dateMs(b[dispatchCol - 1]);
      if (da !== db) return da - db;
    }
    return a.__i - b.__i;  // stable order otherwise
  });

  data.forEach(function (row) { delete row.__i; });
  range.setValues(data);

  applyStatusFormatting(sheet, data, statusCol, dispatchCol, freezeCol, lastCol, DATA_START_ROW, todayMs);
}

/**
 * Apply per-row font color, strikethrough and background to a block of data rows.
 *   font color  -> by status stage (STAGE_FORMAT)
 *   background  -> frozen (green) > overdue In-Process (red) > cleared (banding shows)
 * Shared by sortSheet and the consolidated SUMMARY so they look identical.
 */
function applyStatusFormatting(sheet, data, statusCol, dispatchCol, freezeCol, lastCol, dataStartRow, todayMs) {
  if (!data.length) return;
  var colors = [], lines = [], backgrounds = [];
  for (var i = 0; i < data.length; i++) {
    var stage = statusStage(data[i][statusCol - 1]);
    var fmt = STAGE_FORMAT[stage] || STAGE_FORMAT[6];
    var isFrozen = freezeCol > 0 &&
      String(data[i][freezeCol - 1]).trim().toUpperCase() === 'YES';

    // Overdue = In-Process row whose dispatch date is already past.
    var isOverdue = false;
    if (stage === 1 && dispatchCol > 0) {
      var dv = data[i][dispatchCol - 1];
      var t = (dv instanceof Date) ? dv.getTime() : NaN;
      if (!isNaN(t) && t < todayMs) isOverdue = true;
    }

    // Row background: overdue In-Process is tinted red; otherwise cleared (banding shows).
    var rowBaseBg = isOverdue ? OVERDUE_BG : null;
    // Font: black on overdue rows (readable on red), otherwise the status color.
    var fontColor = isOverdue ? OVERDUE_FONT : fmt.color;

    var rowColors = [], rowLines = [], rowBg = [];
    for (var c = 0; c < lastCol; c++) {
      // Freeze = YES turns only the WORD in the freeze cell green (text color,
      // no cell fill). Every other cell keeps the status font color.
      if (isFrozen && freezeCol > 0 && c === freezeCol - 1) {
        rowColors.push(FREEZE_YES_TEXT);
      } else {
        rowColors.push(fontColor);
      }
      rowLines.push(fmt.strike ? 'line-through' : 'none');
      rowBg.push(rowBaseBg);  // background = banding / overdue pink only (no green fill)
    }
    colors.push(rowColors);
    lines.push(rowLines);
    backgrounds.push(rowBg);
  }
  var rng = sheet.getRange(dataStartRow, 1, data.length, lastCol);
  rng.setFontColors(colors);
  rng.setFontLines(lines);
  rng.setBackgrounds(backgrounds);
}

/* ================= CONSOLIDATED SUMMARY ================= */

/** Category sheets = every tab that has a STATUS column (excludes LEGENDS & SUMMARY). */
function getCategorySheets() {
  return SpreadsheetApp.getActive().getSheets().filter(function (sh) {
    var name = sh.getName();
    if (name === SUMMARY_SHEET || name.toUpperCase() === 'LEGENDS') return false;
    var lastCol = sh.getLastColumn();
    if (lastCol < 1) return false;
    var headers = sh.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0];
    for (var c = 0; c < headers.length; c++) {
      if (String(headers[c]).trim().toUpperCase() === 'STATUS') return true;
    }
    return false;
  });
}

/**
 * Normalize header-name variants so equivalent columns from different sheets
 * line up in ONE consolidated column:
 *   "TENTATIVE DISPATCH DATE" / "DISPATCH DATE"      -> "DISPATCH DATE"
 *   "WORK ORDER FREEZE?" / "TECHNICAL FREEZE?"        -> "FREEZE?"
 * Any other header is kept as-is (just trimmed).
 */
function canonicalHeader(raw) {
  var key = String(raw).trim();
  var u = key.toUpperCase();
  if (u.indexOf('DISPATCH') !== -1) return 'DISPATCH DATE';
  if (u.indexOf('FREEZE') !== -1) return 'FREEZE?';
  return key;
}

/**
 * Collect every data row from all category sheets into one table.
 * Columns are aligned by CANONICAL header name (variants merged, see above),
 * so each value lands in the right column. A "SOURCE SHEET" column is prepended.
 * Returns { headers: [...], rows: [[...], ...] }.
 */
function assembleConsolidated() {
  var cats = getCategorySheets();

  // Build the master header list = union of canonical headers (first-seen order).
  var master = [], pos = {};
  cats.forEach(function (c) {
    var lastCol = c.getLastColumn();
    if (lastCol < 1) return;
    c.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0].forEach(function (h) {
      if (String(h).trim() === '') return;
      var canon = canonicalHeader(h);
      var u = canon.toUpperCase();
      if (!(u in pos)) { pos[u] = master.length; master.push(canon); }
    });
  });

  // Ensure a "Type of Work" column always exists (so we can auto-fill it below).
  if (!('TYPE OF WORK' in pos)) { pos['TYPE OF WORK'] = master.length; master.push('Type of Work'); }
  var towPos = pos['TYPE OF WORK'];

  var headers = ['SOURCE SHEET'].concat(master);
  var rows = [];

  cats.forEach(function (c) {
    var lastCol = c.getLastColumn(), lastRow = c.getLastRow();
    if (lastRow < DATA_START_ROW) return;
    var hdrs = c.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0]
      .map(function (h) { return canonicalHeader(h).toUpperCase(); });
    var vals = c.getRange(DATA_START_ROW, 1, lastRow - DATA_START_ROW + 1, lastCol).getValues();
    vals.forEach(function (row) {
      if (row.every(function (v) { return v === '' || v === null; })) return;  // skip blank rows
      var outRow = [];
      for (var k = 0; k < master.length; k++) outRow.push('');
      for (var i = 0; i < hdrs.length; i++) {
        var p = pos[hdrs[i]];
        if (p !== undefined && outRow[p] === '') outRow[p] = row[i];  // first non-empty wins
      }
      // Auto-fill Type of Work from the source sheet name when it's empty.
      if (outRow[towPos] === '' || outRow[towPos] === null) outRow[towPos] = c.getName().trim();
      rows.push([c.getName()].concat(outRow));
    });
  });

  return { headers: headers, rows: rows };
}

/** Lock a sheet so collaborators can't edit (owner/script still can). */
function lockSheet(sh, desc) {
  var prot = sh.protect().setDescription(desc);
  var editors = prot.getEditors().map(function (e) { return e.getEmail(); });
  if (editors.length) prot.removeEditors(editors);
  if (prot.canDomainEdit()) prot.setDomainEdit(false);
}

// Title banner shown in row 1 of the SUMMARY sheet.
var SUMMARY_TITLE = 'CONSOLIDATED ORDER SUMMARY — ALL SHEETS';

/**
 * Read the date number-format used by the source sheets' dispatch column
 * (e.g. MACHINES uses d/m/yyyy) so the summary can match it instead of
 * falling back to the spreadsheet locale default.
 */
function sourceDateFormat() {
  var cats = getCategorySheets();
  for (var s = 0; s < cats.length; s++) {
    var sh = cats[s];
    if (sh.getLastRow() < DATA_START_ROW) continue;
    var lastCol = sh.getLastColumn();
    var headers = sh.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0];
    for (var c = 0; c < headers.length; c++) {
      if (String(headers[c]).toUpperCase().indexOf('DISPATCH') !== -1) {
        return sh.getRange(DATA_START_ROW, c + 1).getNumberFormat();
      }
    }
  }
  return 'd/m/yyyy';
}

// Preferred column widths (px) keyed by UPPERCASE header. _default is used
// for anything not listed. Tuned so headers never break mid-word and long
// text wraps within a comfortable width.
var COLUMN_WIDTHS = {
  'SOURCE SHEET': 120,
  'CUSTOMER NAME': 180,
  'MACHINE NAME': 300,
  'QTY': 70,
  'MACHINE DIRECTION': 150,
  'PROJECT NO (IF APPLI.)': 130,
  'WORK ORDER NO': 130,
  'DISPATCH DATE': 110,
  'STATUS': 130,
  'FREEZE?': 100,
  'SERIAL NO': 170,
  'ELE SERIAL NO': 120,
  'INVOICE NO': 120,
  'INVOICE DATE': 110,
  'TYPE OF WORK': 120,
  '_default': 130
};

/**
 * Write & format the consolidated table on the SUMMARY sheet, styled like the
 * category tabs: title banner (row 1), header row (row 2), data from row 3,
 * status font colors, freeze/overdue backgrounds, banding and borders.
 *   full = true  -> also auto-size columns (used on a full build)
 */
function writeConsolidated(sh, full) {
  var d = assembleConsolidated();
  var nCols = d.headers.length;
  var nRows = d.rows.length;

  // Detect key columns by header name
  var statusCol = 0, dispatchCol = 0, freezeCol = 0;
  for (var c = 0; c < d.headers.length; c++) {
    var h = String(d.headers[c]).trim().toUpperCase();
    if (h === 'STATUS') statusCol = c + 1;
    if (h.indexOf('DISPATCH') !== -1) dispatchCol = c + 1;
    if (h.indexOf('FREEZE') !== -1) freezeCol = c + 1;
  }

  // Sort the ENTIRE summary by STATUS stage first (In-Process, Completed,
  // Disputed, Dispatched, Cancelled), then by dispatch date ascending within
  // each status — across all sheets, regardless of type of work.
  d.rows.sort(function (a, b) {
    var sa = statusCol ? statusStage(a[statusCol - 1]) : 0;
    var sb = statusCol ? statusStage(b[statusCol - 1]) : 0;
    if (sa !== sb) return sa - sb;
    if (dispatchCol) return dateMs(a[dispatchCol - 1]) - dateMs(b[dispatchCol - 1]);
    return 0;
  });

  // Reset content / merges / banding so a re-write is clean
  sh.getRange(1, 1, 2, sh.getMaxColumns()).breakApart();
  sh.clear();
  sh.getBandings().forEach(function (b) { b.remove(); });

  // --- Title banner (row 1) ---
  sh.getRange(1, 1).setValue(SUMMARY_TITLE);
  sh.getRange(1, 1, 1, nCols).merge()
    .setFontFamily('Arial').setFontSize(14).setFontWeight('bold')
    .setHorizontalAlignment('center').setVerticalAlignment('middle')
    .setBackground('#38761D').setFontColor('#FFFFFF');
  sh.setRowHeight(1, 32);

  // --- Header row (row 2) ---
  sh.getRange(2, 1, 1, nCols).setValues([d.headers])
    .setFontFamily('Arial').setFontWeight('bold')
    .setHorizontalAlignment('center').setVerticalAlignment('middle')
    .setWrap(true).setBackground('#B6D7A8').setFontColor('#000000');

  // --- Data (row 3+) ---
  if (nRows) {
    var dataRange = sh.getRange(3, 1, nRows, nCols);
    dataRange.setValues(d.rows).setFontFamily('Arial').setFontSize(10)
      .setVerticalAlignment('top').setWrap(true);
    dataRange.applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);

    if (statusCol) {
      var today = new Date(); today.setHours(0, 0, 0, 0);
      applyStatusFormatting(sh, d.rows, statusCol, dispatchCol, freezeCol, nCols, 3, today.getTime());
    }

    // Match the source sheets' date format so dispatch/invoice dates display
    // the same way as on MACHINES (not the locale default). sh.clear() above
    // wipes formats, so this must run on every write.
    var dateFmt = sourceDateFormat();
    for (var dc = 0; dc < d.headers.length; dc++) {
      if (String(d.headers[dc]).toUpperCase().indexOf('DATE') !== -1) {
        sh.getRange(3, dc + 1, nRows, 1).setNumberFormat(dateFmt);
      }
    }
  }

  // --- Borders, freeze panes, widths ---
  sh.getRange(1, 1, Math.max(nRows + 2, 2), nCols)
    .setBorder(true, true, true, true, true, true, '#999999', SpreadsheetApp.BorderStyle.SOLID);
  sh.setFrozenRows(2);
  // Note: no frozen columns — the merged title banner in row 1 spans all
  // columns, and freezing a column would split that merge (Sheets errors).

  if (full) {
    for (var col = 1; col <= nCols; col++) {
      var key = String(d.headers[col - 1]).trim().toUpperCase();
      sh.setColumnWidth(col, COLUMN_WIDTHS[key] || COLUMN_WIDTHS._default);
    }
  }
  return nRows;
}

/** Build (or rebuild) the locked, consolidated SUMMARY sheet. */
function buildSummary() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(SUMMARY_SHEET);
  if (!sh) sh = ss.insertSheet(SUMMARY_SHEET, 0);

  sh.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(function (p) { p.remove(); });
  sh.getCharts().forEach(function (ch) { sh.removeChart(ch); });  // remove any leftover charts

  var n = writeConsolidated(sh, true);
  lockSheet(sh, 'Auto-generated consolidated summary — locked');
  ss.toast('Summary consolidated (' + n + ' rows).', 'Summary', 4);
}

/**
 * Refresh used on edits / open: rewrites the consolidated data in place.
 * Protection stays intact (the owner/script can still write to it).
 */
function refreshSummary() {
  var sh = SpreadsheetApp.getActive().getSheetByName(SUMMARY_SHEET);
  if (!sh) return;          // not built yet
  writeConsolidated(sh, false);
}

/* ================= MACHINES COLUMN SETUP ================= */

/** Build a dropdown (list) validation rule. */
function listRule(values) {
  return SpreadsheetApp.newDataValidation()
    .requireValueInList(values, true)   // true = show dropdown arrow
    .setAllowInvalid(false)
    .build();
}

/**
 * Configure the MACHINES tab's columns (run from the menu):
 *   - "PANEL IN-HOUSE OR OUTSOURCE?"  -> dropdown: In-House / Outsource
 *   - "CONTROL PANEL STATUS"          -> dropdown: Panel Design Stage /
 *                                        BOM & Wiring Diagram / Assembly Stage / Completed
 *   - "QTY"                           -> plain manual entry (dropdown removed)
 *   - "PROJECT NO..."                 -> plain manual entry (dropdown removed)
 * Columns are matched by header name, so they must already exist on the tab.
 */
function setupMachineColumns() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName('MACHINES');
  if (!sh) { ss.toast('No MACHINES tab found.', 'Setup', 4); return; }

  var lastCol = sh.getLastColumn();
  var startRow = DATA_START_ROW;                 // 3
  var nRows = sh.getMaxRows() - startRow + 1;     // apply down the whole column
  if (nRows < 1) return;

  var headers = sh.getRange(HEADER_ROW, 1, 1, lastCol).getValues()[0];
  var applied = [];

  for (var c = 0; c < headers.length; c++) {
    var u = String(headers[c]).trim().toUpperCase();
    var rng = sh.getRange(startRow, c + 1, nRows, 1);

    if (u.indexOf('OUTSOURCE') !== -1 || u.indexOf('IN-HOUSE') !== -1) {
      rng.setDataValidation(listRule(['In-House', 'Outsource']));
      applied.push('Panel In-House/Outsource dropdown');
    } else if (u.indexOf('CONTROL') !== -1 && (u.indexOf('PANEL') !== -1 || u.indexOf('PANNEL') !== -1)) {
      rng.setDataValidation(listRule(['Panel Design Stage', 'BOM & Wiring Diagram', 'Assembly Stage', 'Completed']));
      applied.push('Control Panel Status dropdown');
    } else if (u === 'QTY') {
      rng.setDataValidation(null);               // remove list -> manual entry
      applied.push('QTY set to manual');
    } else if (u.indexOf('PROJECT') !== -1) {
      rng.setDataValidation(null);               // remove list -> manual entry
      applied.push('Project No set to manual');
    }
  }

  ss.toast(applied.length ? ('Done: ' + applied.join('; ')) : 'No matching columns found on MACHINES.',
    'Machines setup', 6);
}

/* ---------------- triggers ---------------- */

/**
 * Run this ONCE manually to (re)install the automatic triggers:
 *  - onEdit : re-sort the sheet you just edited
 *  - onOpen : re-sort everything when the file is opened
 */
function installTrigger() {
  var ss = SpreadsheetApp.getActive();
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    var fn = triggers[i].getHandlerFunction();
    if (fn === 'onEditAutoSort' || fn === 'autoSortOnOpen') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('onEditAutoSort').forSpreadsheet(ss).onEdit().create();
  ScriptApp.newTrigger('autoSortOnOpen').forSpreadsheet(ss).onOpen().create();
  sortAllSheets();  // sort immediately so existing data is fixed right now
  buildSummary();   // create the locked dashboard now
  ss.toast('Auto-sort + summary installed and applied!', 'Setup', 5);
}

/** Simple trigger: adds a menu every time the file opens. */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('⚙️ Auto-Sort')
    .addItem('Sort all sheets now', 'sortAllSheets')
    .addItem('Rebuild consolidated summary', 'buildSummary')
    .addItem('Set up Machines dropdowns', 'setupMachineColumns')
    .addToUi();
}

/** Installable open trigger: re-sorts and refreshes the dashboard on open. */
function autoSortOnOpen() {
  sortAllSheets();
  refreshSummary();
}

/**
 * Edit trigger handler. Bound to the INSTALLABLE trigger only (created by
 * installTrigger), so it always runs as the owner — even for edits made by
 * shared collaborators. There is intentionally NO simple onEdit(e) function:
 * a simple trigger would run as the editing collaborator and fail trying to
 * write the protected SUMMARY sheet.
 */
function onEditAutoSort(e) {
  var sheet = (e && e.range) ? e.range.getSheet() : SpreadsheetApp.getActiveSheet();
  if (sheet.getName() === SUMMARY_SHEET) return;  // ignore edits on the summary itself
  sortSheet(sheet);
  try {
    refreshSummary();   // keep the consolidated summary live
  } catch (err) {
    // never let a summary issue block the sort
  }
}

/** Sort every tab at once. Used by the menu, the open trigger, and install. */
function sortAllSheets() {
  SpreadsheetApp.getActive().getSheets().forEach(sortSheet);
}
