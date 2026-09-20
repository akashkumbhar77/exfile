    // Rule ${number}: ${rule_id} - ${tab_note}
    try {
      var ${var_name} = ${fn}(ss, today);
      if (${var_name}.rowsAffected === 0) notes.push(${var_name}.tab + ': no rows to consolidate');
    } catch (err) {
      notes.push('rule ${rule_id} failed - ' + err.message);
    }
