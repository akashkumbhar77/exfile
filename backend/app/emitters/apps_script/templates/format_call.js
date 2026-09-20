    // Rule ${number}: ${rule_id} - ${tab_note}
    if (${selector}) {
      try {
        var ${var_name} = ${fn}(view, today);
        if (${var_name} && ${var_name}.rowsAffected) {
          ${var_name}.range.setFontColors(${var_name}.fonts);
          ${var_name}.range.setBackgrounds(${var_name}.fills);
          ${var_name}.range.setFontLines(${var_name}.lines);
        }
      } catch (err) {
        notes.push(name + ': rule ${rule_id} failed - ' + err.message);
      }
    }
