      var ${var_name} = ${fn}(view);
      run.guardedRows += ${var_name}.rowsAffected;
      if (run.guardedRows > MAX_ROWS_PER_RUN) {
        run.fail(name + ': ' + ${var_name}.rule + ' stopped - it would move more than ' +
                 MAX_ROWS_PER_RUN + ' rows in one run');
      } else if (${var_name}.rowsAffected) {
        view.values = ${var_name}.values;
        run.writes.push(writeValues_(view, ${var_name}.values));
      }
