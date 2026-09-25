/** Rule ${number} (${rule_id}) on ${tab_note}: planned in memory, written at the end of the run. */
function ${step}(run, scope) {
  for (var v = 0; v < run.views.length; v++) {
    var view = run.views[v], name = view.name;
    if (!view.rowCount || (scope !== null && scope !== name) || !(${selector})) continue;
    try {
${call}
    } catch (err) {
      run.fail(name + ': rule ${rule_id} failed - ' + err.message);
    }
  }
}
