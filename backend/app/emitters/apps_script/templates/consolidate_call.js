/**
 * Rule ${number} (${rule_id}) builds ${target_note} from ${tab_note}.
 * Planned in memory; written only at the end of the run, once every rule has succeeded.
 */
function ${step}(run, scope) {
  try {
    var ${var_name} = ${fn}(run);
    if (${var_name}.rowsAffected === 0) run.notes.push(${var_name}.tab + ': no rows to consolidate');
    run.writes.push(${var_name}.apply);
  } catch (err) {
    run.fail('rule ${rule_id} failed - ' + err.message);
  }
}
