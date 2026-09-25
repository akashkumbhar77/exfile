      var ${var_name} = ${fn}(view);
      if (${var_name} && ${var_name}.rows.length) {
        run.writes.push(writeDropdowns_(view, ${var_name}.column, ${var_name}.rows, ${values}, ${allow_invalid}));
      }
