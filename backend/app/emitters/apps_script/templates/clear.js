/**
 * ${headline}
 * ${when}
 */
function ${fn}(view, today) {
${columns}
  var targets = [];
  var named = [${targets}];
  for (var n = 0; n < named.length; n++) if (named[n] !== undefined) targets.push(named[n]);
  var out = view.values.slice(), rows = [], cells = [];
  for (var i = 0; i <= lastContentIndex_(view); i++) {
    var row = view.values[i];
    if (rowIsEmpty_(row) || isHeld_(view, row)) continue;
    if (!${condition}) continue;
    var any = false;
    for (var t = 0; t < targets.length; t++) if (!isEmpty_(row[targets[t]])) any = true;
    if (!any) continue;                          // nothing to clear in this row
    var blanked = row.slice();
    for (var t2 = 0; t2 < targets.length; t2++) {
      blanked[targets[t2]] = '';
      cells.push([i, targets[t2]]);
    }
    out[i] = blanked;
    rows.push(i);
  }
  return { rule: '${rule_id}', action: 'clear', tab: view.name, rowsAffected: rows.length,
           rows: rows, cells: cells, values: out };
}
