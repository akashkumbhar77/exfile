/**
 * ${headline}
 * ${when}
 */
function ${fn}(view) {
  var col = view.cols[${column}];
  if (col === undefined) return null;          // this tab has no ${column_name} column: left alone
  var rows = [];
  for (var i = 0; i <= lastContentIndex_(view); i++) {
    if (isHeld_(view, view.values[i])) continue;   // held rows keep whatever dropdown they have
    rows.push(i);
  }
  // Values already there that are not in the list are left as they are, never changed.
  return { rule: '${rule_id}', action: 'validate', tab: view.name, rowsAffected: rows.length,
           column: col, rows: rows };
}
