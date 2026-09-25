/**
 * ${headline}
 * ${when}
 *
 * Moved rows are copied to the backup tab first. If a moved row has a value in a column
 * ${to_tab_name} does not have, the whole run stops rather than drop it.
 */
function ${step}(run, scope) {
  var today = run.today;
  try {
    var target = viewNamed_(run, ${to_tab});
    if (!target) throw new Error('the tab ' + ${to_tab} + ' is missing or not governed');
    var at = ${at};
    // Work out every move before changing anything, so a refusal leaves no half-move behind.
    var moves = [], incoming = [];
    for (var v = 0; v < run.views.length; v++) {
      var view = run.views[v], name = view.name;
      if (name === ${to_tab} || (scope !== null && scope !== name) || !(${selector})) continue;
${columns}
      var taken = [];
      for (var i = 0; i <= lastContentIndex_(view); i++) {
        var row = view.values[i];
        if (rowIsEmpty_(row) || isHeld_(view, row)) continue;
        if (!${condition}) continue;
        incoming.push(toTarget_(row, view, target));
        taken.push(i);
      }
      if (taken.length) moves.push({ view: view, rows: taken });
    }
    run.guardedRows += 2 * incoming.length;      // rows taken out, plus rows put in
    if (run.guardedRows > MAX_ROWS_PER_RUN) {
      run.fail('rule ${rule_id} stopped - it would move more than ' + MAX_ROWS_PER_RUN + ' rows in one run');
      return;
    }
    for (var m = 0; m < moves.length; m++) {
      for (var b = 0; b < moves[m].rows.length; b++) backUp_(run, '${rule_id}', moves[m].view, moves[m].rows[b]);
      deleteDataRows_(run, moves[m].view, moves[m].rows);
    }
    if (incoming.length) insertDataRows_(run, target, at, incoming);
  } catch (err) {
    run.fail('rule ${rule_id} failed - ' + err.message);
  }
}
