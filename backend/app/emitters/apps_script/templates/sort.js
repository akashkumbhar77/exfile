/**
 * ${headline}
 * ${when}
 */
function ${fn}(view) {
${columns}
  var rows = view.values;
  var slots = [], movable = [];
  // Down to the last row with anything in it: rows an earlier rule emptied stay where they are.
  for (var i = 0; i <= lastContentIndex_(view); i++) {
    if (isHeld_(view, rows[i])) continue;      // held rows keep their exact position
    slots.push(i);
    movable.push({ row: rows[i], at: i });
  }
  movable.sort(function (a, b) {
    var d = 0;
${comparisons}
    return a.at - b.at;                        // ties keep the order they were already in
  });
  var out = rows.slice(), moved = 0;
  for (var k = 0; k < slots.length; k++) {
    if (movable[k].at !== slots[k]) moved++;
    out[slots[k]] = movable[k].row;
  }
  return { rule: '${rule_id}', action: 'sort', tab: view.name, rowsAffected: moved, values: out };
}
