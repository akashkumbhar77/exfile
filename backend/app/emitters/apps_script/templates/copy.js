/**
 * ${headline}
 * ${when}
 *
 * A row is copied once: rows whose ${key_note} already appear in ${to_tab_name} are skipped, and so
 * are rows with nothing in those columns. If a copied row has a value in a column ${to_tab_name}
 * does not have, the whole run stops rather than drop it.
 */
function ${step}(run, scope) {
  var today = run.today;
  try {
    var target = viewNamed_(run, ${to_tab});
    if (!target) throw new Error('the tab ' + ${to_tab} + ' is missing or not governed');
    var keys = ${keys};
    for (var k = 0; k < keys.length; k++) {
      if (!(keys[k] in target.cols)) throw new Error('the tab ' + ${to_tab} + ' has no ' + keys[k] + ' column');
    }
    var existing = {};
    for (var t = 0; t <= lastContentIndex_(target); t++) {
      var had = keyOf_(target.values[t], target, keys);
      if (had !== null) existing[had] = true;
    }
    var incoming = [];
    for (var v = 0; v < run.views.length; v++) {
      var view = run.views[v], name = view.name;
      if (name === ${to_tab} || (scope !== null && scope !== name) || !(${selector})) continue;
${columns}
      for (var i = 0; i <= lastContentIndex_(view); i++) {
        var row = view.values[i];
        if (rowIsEmpty_(row) || isHeld_(view, row)) continue;
        if (!${condition}) continue;
        var key = keyOf_(row, view, keys);
        if (key === null || existing[key]) continue;   // nothing to match on, or already there
        existing[key] = true;
        incoming.push(toTarget_(row, view, target));
      }
    }
    run.guardedRows += incoming.length;
    if (run.guardedRows > MAX_ROWS_PER_RUN) {
      run.fail('rule ${rule_id} stopped - it would copy more than ' + MAX_ROWS_PER_RUN + ' rows in one run');
      return;
    }
    if (incoming.length) insertDataRows_(run, target, ${at}, incoming);
  } catch (err) {
    run.fail('rule ${rule_id} failed - ' + err.message);
  }
}
