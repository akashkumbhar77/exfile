      var ${var_name} = ${fn}(view);
      if (${var_name}) {
        run.guardedRows += ${var_name}.rowsAffected;
        if (run.guardedRows > MAX_ROWS_PER_RUN) {
          run.fail(name + ': ' + ${var_name}.rule + ' stopped - it would remove more than ' +
                   MAX_ROWS_PER_RUN + ' rows in one run');
        } else if (${var_name}.drop.length) {
          for (var bd = 0; bd < ${var_name}.drop.length; bd++) backUp_(run, '${rule_id}', view, ${var_name}.drop[bd]);
          deleteDataRows_(run, view, ${var_name}.drop);
        }
      }
