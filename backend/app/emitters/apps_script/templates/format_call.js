      var ${var_name} = ${fn}(view, run.today);
      if (${var_name} && ${var_name}.rowsAffected) {
        view.formats = ${var_name};
        run.writes.push(writeFormats_(${var_name}));
      }
