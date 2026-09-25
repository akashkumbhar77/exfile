/**
 * ${headline}
 * ${when}
 */
function ${fn}(view, today) {
${columns}
  var rows = view.values;
  if (!rows.length) return null;
  var current = viewFormats_(view);            // as earlier rules in this run left them
  var range = current.range;
  var fonts = current.fonts.slice(), fills = current.fills.slice(), lines = current.lines.slice();
  var changed = 0;
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    if (isHeld_(view, row)) continue;          // held rows keep the formatting they have
    var font = fonts[i].slice(), fill = fills[i].slice(), line = lines[i].slice();
${body}
    if (!sameRow_(font, fonts[i]) || !sameRow_(fill, fills[i]) || !sameRow_(line, lines[i])) changed++;
    fonts[i] = font; fills[i] = fill; lines[i] = line;
  }
  return { rule: '${rule_id}', action: 'format', tab: view.name, rowsAffected: changed,
           range: range, fonts: fonts, fills: fills, lines: lines };
}
