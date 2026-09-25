      var ${var_name} = ${fn}(view, run.today);
      run.guardedRows += ${var_name}.rowsAffected;
      if (run.guardedRows > MAX_ROWS_PER_RUN) {
        run.fail(name + ': ' + ${var_name}.rule + ' stopped - it would clear more than ' +
                 MAX_ROWS_PER_RUN + ' rows in one run');
      } else if (${var_name}.rows.length) {
        for (var bc = 0; bc < ${var_name}.rows.length; bc++) backUp_(run, '${rule_id}', view, ${var_name}.rows[bc]);
        view.values = ${var_name}.values;
        run.writes.push(writeRows_(view, ${var_name}.rows));
      }
