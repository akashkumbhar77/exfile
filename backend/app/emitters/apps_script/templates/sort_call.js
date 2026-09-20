    // Rule ${number}: ${rule_id} - ${tab_note}
    if (${selector}) {
      try {
        var ${var_name} = ${fn}(view);
        guardedRows += ${var_name}.rowsAffected;
        if (guardedRows > MAX_ROWS_PER_RUN) {
          notes.push(name + ': ' + ${var_name}.rule + ' stopped - it would move more than ' +
                     MAX_ROWS_PER_RUN + ' rows in one run');
        } else if (${var_name}.rowsAffected) {
          view.sheet.getRange(DATA_START_ROW, 1, ${var_name}.values.length, view.lastCol)
              .setValues(${var_name}.values);
          view.values = ${var_name}.values;
        }
      } catch (err) {
        notes.push(name + ': rule ${rule_id} failed - ' + err.message);
      }
    }
