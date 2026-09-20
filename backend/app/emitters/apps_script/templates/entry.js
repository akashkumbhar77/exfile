// ---------------------------------------------------------------- entry point

/**
 * Applies every rule above, in order, to each governed tab.
 *
 * Nothing is touched until every governed tab's header row still matches the fingerprint taken
 * when this script was generated. If any of them changed, the whole run stops and says so,
 * rather than reorganising some tabs against names the rules no longer understand.
 */
function ${entry_point}() {
  var ss = SpreadsheetApp.getActive();
  var today = todayStart_(ss);
  var guardedRows = 0;
  var notes = [];

  var views = [];
  for (var t = 0; t < GOVERNED_TABS.length; t++) {
    var tabName = GOVERNED_TABS[t].tab;
    var sheet = ss.getSheetByName(tabName);
    if (!sheet) {
      notes.push(tabName + ': tab not found');
      continue;
    }
    var checking = buildView_(sheet);
    if (headerHash_(checking) !== GOVERNED_TABS[t].headerHash) {
      notes.push(tabName + ': the header row changed since this script was generated');
      continue;
    }
    views.push(checking);
  }
  if (notes.length) {                          // pre-flight failed: nothing runs at all
    notes.push('nothing was changed - regenerate the script for the sheet as it is now');
    report_(notes);
    return;
  }

  for (var v = 0; v < views.length; v++) {
    var view = views[v];
    var name = view.name;
    if (!view.rowCount) continue;

${calls}
  }
  report_(notes);
}

function report_(notes) {
  var ss = SpreadsheetApp.getActive();
  if (!notes.length) {
    ss.toast('Finished. Everything is organised.', PRODUCT, 5);
    return;
  }
  ss.toast(notes.join('  |  '), PRODUCT + ' - ' + notes.length + ' thing(s) to look at', 30);
}
