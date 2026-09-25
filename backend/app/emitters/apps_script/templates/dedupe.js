/**
 * ${headline}
 * ${when}
 */
function ${fn}(view) {
  var keys = ${keys};
  for (var k = 0; k < keys.length; k++) if (!(keys[k] in view.cols)) return null;   // tab skipped
  var candidates = [];
  for (var i = 0; i <= lastContentIndex_(view); i++) {
    var row = view.values[i];
    if (rowIsEmpty_(row) || isHeld_(view, row)) continue;   // never removed, never the original
    var key = keyOf_(row, view, keys);
    if (key !== null) candidates.push({ at: i, key: key });
  }
${keep}
  var seen = {}, drop = [];
  for (var c = 0; c < candidates.length; c++) {
    if (seen[candidates[c].key]) drop.push(candidates[c].at);
    seen[candidates[c].key] = true;
  }
  drop.sort(function (a, b) { return a - b; });
  return { rule: '${rule_id}', action: 'dedupe', tab: view.name, rowsAffected: drop.length, drop: drop };
}
