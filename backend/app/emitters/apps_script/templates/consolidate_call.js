/** Rule ${number} (${rule_id}) builds ${target_note} from ${tab_note}; written at the end of the run. */
function ${step}(run, scope) {
  try {
    var ${var_name} = ${fn}(run);
    if (${var_name}.rowsAffected === 0) run.notes.push(${var_name}.tab + ': no rows to consolidate');
    run.writes.push(${var_name}.apply);
  } catch (err) {
    run.fail('rule ${rule_id} failed - ' + err.message);
  }
}
