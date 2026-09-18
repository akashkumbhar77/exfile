/**********************************************************************
 * CompareWorkbooks.gs — M2 acceptance helper (NOT part of the engine).
 *
 * Paste into a STANDALONE Apps Script project (script.google.com > New project),
 * set the two spreadsheet IDs below, run `compareWorkbooks`, read the log.
 *
 *   A = copy of the reference workbook running legacy.gs   (after installTrigger)
 *   B = copy of the reference workbook running Engine.gs   (after Run all rules now)
 *
 * Compares every tab present in A (engine bookkeeping tabs in B are ignored):
 * values, font colours, strikethrough/underline, fills, number formats, font
 * weights, merges, frozen rows, column widths and sheet protection.
 **********************************************************************/

var WORKBOOK_A_LEGACY = 'PASTE_LEGACY_COPY_ID';
var WORKBOOK_B_ENGINE = 'PASTE_ENGINE_COPY_ID';
var MAX_REPORTED_DIFFS = 50;

function compareWorkbooks() {
  var a = SpreadsheetApp.openById(WORKBOOK_A_LEGACY);
  var b = SpreadsheetApp.openById(WORKBOOK_B_ENGINE);
  var diffs = [];
  function add(msg) { if (diffs.length < MAX_REPORTED_DIFFS) diffs.push(msg); }

  var namesA = a.getSheets().map(function (s) { return s.getName(); });
  var namesB = b.getSheets().map(function (s) { return s.getName(); })
    .filter(function (n) { return n !== '_config' && n !== '_engine_snapshots'; });
  if (namesA.join('|') !== namesB.join('|')) add('tab order: ' + namesA.join(',') + ' vs ' + namesB.join(','));

  namesA.forEach(function (name) {
    var sa = a.getSheetByName(name);
    var sb = b.getSheetByName(name);
    if (!sb) { add(name + ': missing in engine copy'); return; }
    var rows = Math.max(sa.getLastRow(), sb.getLastRow(), 1);
    var cols = Math.max(sa.getLastColumn(), sb.getLastColumn(), 1);
    var ra = sa.getRange(1, 1, rows, cols);
    var rb = sb.getRange(1, 1, rows, cols);
    var grids = [
      ['value', ra.getValues(), rb.getValues()],
      ['font colour', ra.getFontColors(), rb.getFontColors()],
      ['font line', ra.getFontLines(), rb.getFontLines()],
      ['fill', ra.getBackgrounds(), rb.getBackgrounds()],
      ['number format', ra.getNumberFormats(), rb.getNumberFormats()],
      ['font weight', ra.getFontWeights(), rb.getFontWeights()]
    ];
    grids.forEach(function (g) {
      for (var r = 0; r < rows; r++) {
        for (var c = 0; c < cols; c++) {
          var x = norm(g[1][r][c]);
          var y = norm(g[2][r][c]);
          if (x !== y) add(name + ' ' + g[0] + ' at R' + (r + 1) + 'C' + (c + 1) + ': legacy=' + x + ' engine=' + y);
        }
      }
    });
    var ma = sa.getRange(1, 1, rows, cols).getMergedRanges().map(function (m) { return m.getA1Notation(); }).sort();
    var mb = sb.getRange(1, 1, rows, cols).getMergedRanges().map(function (m) { return m.getA1Notation(); }).sort();
    if (ma.join() !== mb.join()) add(name + ' merges: ' + ma.join() + ' vs ' + mb.join());
    if (sa.getFrozenRows() !== sb.getFrozenRows()) add(name + ' frozen rows: ' + sa.getFrozenRows() + ' vs ' + sb.getFrozenRows());
    for (var c2 = 1; c2 <= cols; c2++) {
      if (sa.getColumnWidth(c2) !== sb.getColumnWidth(c2)) {
        add(name + ' width col ' + c2 + ': ' + sa.getColumnWidth(c2) + ' vs ' + sb.getColumnWidth(c2));
      }
    }
    var pa = sa.getProtections(SpreadsheetApp.ProtectionType.SHEET).length > 0;
    var pb = sb.getProtections(SpreadsheetApp.ProtectionType.SHEET).length > 0;
    if (pa !== pb) add(name + ' protected: ' + pa + ' vs ' + pb);
  });

  if (diffs.length) {
    Logger.log('PARITY FAILED — first ' + diffs.length + ' difference(s):\n' + diffs.join('\n'));
  } else {
    Logger.log('PARITY OK — every tab identical (values, fonts, strikethrough, fills, formats, SUMMARY).');
  }
  return diffs;
}

function norm(v) {
  if (Object.prototype.toString.call(v) === '[object Date]') return 'date:' + v.getTime();
  if (typeof v === 'string' && /^#[0-9a-f]{6}$/i.test(v)) return v.toLowerCase();
  return String(v);
}
