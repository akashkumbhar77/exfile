      var ${var_name} = ${fn}(view, run.today);
      if (${var_name} && ${var_name}.rowsAffected) {
        for (var fr = 0; fr < ${var_name}.fonts.length; fr++) {   // keep the run's picture in step
          view.formats.fonts[fr] = ${var_name}.fonts[fr];
          view.formats.fills[fr] = ${var_name}.fills[fr];
          view.formats.lines[fr] = ${var_name}.lines[fr];
        }
        run.writes.push(writeFormats_(view, ${var_name}));
      }
