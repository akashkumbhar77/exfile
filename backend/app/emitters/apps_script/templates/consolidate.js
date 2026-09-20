/**
 * ${headline}
 * ${when}
 *
 * The target tab is rebuilt from its sources every run, so anything typed into it by hand is
 * replaced. That is why it is locked.
 */
function ${fn}(ss, today) {
  var views = [];
  for (var t = 0; t < GOVERNED_TABS.length; t++) {
    var name = GOVERNED_TABS[t].tab;
    if (name === ${target}) continue;                 // a target is never its own source
    var sheet = ss.getSheetByName(name);
    if (!sheet) continue;
    var view = buildView_(sheet);
    if (${selector}) views.push(view);
  }

  // Master header: every column the sources have, in the order they were first seen.
  var headers = [], at = {};
  for (var v = 0; v < views.length; v++) {
    var order = views[v].order;
    for (var c = 0; c < order.length; c++) {
      if (!(order[c].key in at)) {
        at[order[c].key] = headers.length;
        headers.push(order[c].name);
      }
    }
  }
${derived_columns}
  var leading = ${prepend_names};                     // filled with the source tab's name
  var allHeaders = leading.concat(headers);

  var rows = [];
  for (var v2 = 0; v2 < views.length; v2++) {
    var from = views[v2];
    for (var r = 0; r < from.values.length; r++) {
      var row = from.values[r];
      if (rowIsEmpty_(row) || isHeld_(from, row)) continue;   // blank and held rows are skipped
      var out = [];
      for (var b = 0; b < headers.length; b++) out.push(null);
      for (var o = 0; o < from.order.length; o++) {
        var dst = at[from.order[o].key];
        if (isEmpty_(out[dst])) out[dst] = row[from.order[o].at];   // first non-empty value wins
      }
${derived_fill}
      var prefix = [];
      for (var p = 0; p < leading.length; p++) prefix.push(from.name);
      rows.push(prefix.concat(out));
    }
  }
${sorting}
  var sheet = targetSheet_(ss, ${target});
  var first = sheet.getRange(1, 1);
  var banner = { value: first.getValues()[0][0], font: first.getFontColors()[0][0],
                 background: first.getBackgrounds()[0][0] };
  var wanted = ${data_start} - 1 + rows.length;
  sheet.clear();
  if (sheet.getMaxRows() < Math.max(wanted, 1)) sheet.insertRowsAfter(sheet.getMaxRows(), wanted - sheet.getMaxRows());
  if (sheet.getMaxColumns() < allHeaders.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), allHeaders.length - sheet.getMaxColumns());
  }
${title}
  var headerRange = sheet.getRange(HEADER_ROW, 1, 1, allHeaders.length);
  headerRange.setValues([allHeaders]);
  headerRange.setFontColors([fillArray_(allHeaders.length, ${header_font})]);
  headerRange.setBackgrounds([fillArray_(allHeaders.length, ${header_background})]);
  if (rows.length) sheet.getRange(${data_start}, 1, rows.length, allHeaders.length).setValues(rows);
${locking}
${formatting}
  return { rule: '${rule_id}', action: 'consolidate', tab: ${target}, rowsAffected: rows.length };
}
