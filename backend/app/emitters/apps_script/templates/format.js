/**
 * ${headline}
 * ${when}
 */
function ${fn}(view, today) {
${columns}
  // Every data row, and rows an earlier rule in this run emptied (down to where the data reached
  // when the run started, but never past the rows the tab still has).
  var span = Math.max(lastContentIndex_(view) + 1, Math.min(view.extent, view.values.length));
  if (!span) return null;
  var current = viewFormats_(view);            // as earlier rules in this run left them
  var fonts = current.fonts.slice(0, span), fills = current.fills.slice(0, span);
  var lines = current.lines.slice(0, span);
  var changed = 0;
  for (var i = 0; i < span; i++) {
    var row = view.values[i];
    if (isHeld_(view, row)) continue;          // held rows keep the formatting they have
    var font = fonts[i].slice(), fill = fills[i].slice(), line = lines[i].slice();
${body}
    if (!sameRow_(font, fonts[i]) || !sameRow_(fill, fills[i]) || !sameRow_(line, lines[i])) changed++;
    fonts[i] = font; fills[i] = fill; lines[i] = line;
  }
  return { rule: '${rule_id}', action: 'format', tab: view.name, rowsAffected: changed,
           fonts: fonts, fills: fills, lines: lines };
}
