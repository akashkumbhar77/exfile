/**
 * Column names are matched by their text, never by position, so inserting or reordering
 * columns does not break the rules.
 */
function canonicalHeader_(raw) {
  var v = normText_(raw);
${branches}
  return cellText_(raw).trim();
}
