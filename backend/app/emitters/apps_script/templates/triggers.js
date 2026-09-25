// ---------------------------------------------------------------- when each rule runs

/**
 * Every rule, in order, with what starts it:
 *   on_edit    - someone edits one of the rule's tabs (in one of the listed columns, if any)
 *   debounced  - the same, but only once the tabs have been quiet for a while
 *   schedule   - once in each hour it names, in this script's timezone; Google picks the minute
 *                an hourly trigger fires at, so it runs within that hour, not at an exact time
 *   after      - straight after the rule it names, on the same tabs
 * "Run now" runs all of them, in this order.
 */
var RULES = [
${rules}
];

// Tabs this script builds itself. Editing them starts nothing: they are rebuilt from their sources.
var CONSOLIDATE_TARGETS = [${targets}];

/** The rules an event starts, each followed by the rules chained after it. */
function selectRules_(event, edited) {
  var roots = [], dirty = [];
  for (var i = 0; i < RULES.length; i++) {
    var rule = RULES[i];
    if (rule.trigger === 'after') continue;
    if (event.kind === 'edit') {
      if (rule.trigger !== 'on_edit' && rule.trigger !== 'debounced') continue;
      if (!rule.appliesTo(edited)) continue;
      if (rule.trigger === 'debounced') {        // remembered now, run once things are quiet
        dirty.push(rule.id);
        continue;
      }
      if (rule.columns.length && !editTouches_(edited, event.columns, rule.columns)) continue;
      roots.push({ rule: rule, scope: rule.workbookWide ? null : edited.name });
    } else {
      if (event.ruleIds && event.ruleIds.indexOf(rule.id) < 0) continue;
      if (event.kind !== 'manual' && rule.trigger !== event.kind) continue;
      roots.push({ rule: rule, scope: null });
    }
  }
  var steps = [];
  function fire(rule, scope) {
    steps.push({ rule: rule, scope: scope });
    for (var c = 0; c < RULES.length; c++) {
      if (RULES[c].trigger === 'after' && RULES[c].after === rule.id) fire(RULES[c], scope);
    }
  }
  for (var r = 0; r < roots.length; r++) fire(roots[r].rule, roots[r].scope);
  return { steps: steps, dirty: dirty };
}

/** Whether the edited columns (1-based) include any of the named ones. */
function editTouches_(view, columns, names) {
  for (var o = 0; o < view.order.length; o++) {
    if (columns.indexOf(view.order[o].at + 1) >= 0 && names.indexOf(view.order[o].key) >= 0) return true;
  }
  return false;
}

// ---------------------------------------------------------------- triggers

var HANDLERS = ['handleOpen', 'handleEdit', 'handleTick', 'handleSchedule'];

/**
 * Run this once after pasting the script. It sets up the automatic runs (as you, the owner) and
 * the menu, replacing any triggers an earlier copy of this script - or the code it replaced -
 * left behind. It does not run the rules: use the menu's "Run now" for that.
 */
function installTrigger() {
  var ss = SpreadsheetApp.getActive();
  var triggers = ScriptApp.getProjectTriggers();
  for (var t = 0; t < triggers.length; t++) {
    var fn = triggers[t].getHandlerFunction();
    // Ours, or an orphan whose function no longer exists (it would only fail every time it fires).
    if (HANDLERS.indexOf(fn) >= 0 || typeof globalThis[fn] !== 'function') {
      ScriptApp.deleteTrigger(triggers[t]);
    }
  }
  ScriptApp.newTrigger('handleOpen').forSpreadsheet(ss).onOpen().create();
${install}
  PropertiesService.getScriptProperties().deleteProperty('automation.paused');
  var note = ${installed_note};
  try {
    handleOpen();                                // the menu now, not only after a reload
  } catch (err) {
    note += ' Reload the spreadsheet to see the menu.';
  }
  ss.toast(note, PRODUCT, 15);
}

/** Opening the spreadsheet: the menu. */
function handleOpen() {
  SpreadsheetApp.getUi()
    .createMenu(PRODUCT)
    .addItem('Run now', '${entry_point}')
    .addSeparator()
    .addItem('Pause automatic runs', 'pauseAutomation')
    .addItem('Resume automatic runs', 'resumeAutomation')
    .addToUi();
}

/** An edit by anyone: runs the rules that watch the edited tab and columns. */
function handleEdit(e) {
  try {
    if (paused_() || !e || !e.range) return;
    var columns = [];
    for (var c = e.range.getColumn(); c <= e.range.getLastColumn(); c++) columns.push(c);
    runEvent_({ kind: 'edit', tab: e.range.getSheet().getName(), columns: columns }, false);
  } catch (err) {
    report_(['the automatic run after an edit failed - ' + err.message], true);
  }
}

/** Every minute: runs debounced rules whose tabs have been quiet long enough. */
function handleTick() {
  try {
    if (paused_()) return;
    var due = dueDebounced_();
    if (due.length) runEvent_({ kind: 'debounced', ruleIds: due }, false);
  } catch (err) {
    report_(['a delayed automatic run failed - ' + err.message], true);
  }
}

/** Every hour: runs scheduled rules due in this hour, once each. */
function handleSchedule() {
  try {
    if (paused_()) return;
    var due = dueSchedules_();
    if (!due.ids.length) return;
    if (runEvent_({ kind: 'schedule', ruleIds: due.ids }, false)) {
      var props = PropertiesService.getScriptProperties();
      for (var i = 0; i < due.ids.length; i++) props.setProperty('schedule.' + due.ids[i], due.slot);
    }
  } catch (err) {
    report_(['a scheduled run failed - ' + err.message], true);
  }
}

/** Stops the automatic runs. Nothing is locked: everyone can keep editing, and Run now still works. */
function pauseAutomation() {
  PropertiesService.getScriptProperties().setProperty('automation.paused', '1');
  SpreadsheetApp.getActive().toast('Automatic runs are paused. Run now still works.', PRODUCT, 10);
}

function resumeAutomation() {
  PropertiesService.getScriptProperties().deleteProperty('automation.paused');
  SpreadsheetApp.getActive().toast(
    'Automatic runs are back on. Edits made while paused are picked up the next time the ' +
    'rules run, or use Run now.', PRODUCT, 10);
}

function paused_() {
  return PropertiesService.getScriptProperties().getProperty('automation.paused') === '1';
}

// ---------------------------------------------------------------- debounce and schedule state

function markDirty_(ids) {
  var props = PropertiesService.getScriptProperties();
  for (var i = 0; i < ids.length; i++) props.setProperty('dirty.' + ids[i], String(Date.now()));
}

function clearDirty_(ids) {
  var props = PropertiesService.getScriptProperties();
  for (var i = 0; i < ids.length; i++) props.deleteProperty('dirty.' + ids[i]);
}

function dueDebounced_() {
  var props = PropertiesService.getScriptProperties(), due = [];
  for (var i = 0; i < RULES.length; i++) {
    if (RULES[i].trigger !== 'debounced') continue;
    var at = props.getProperty('dirty.' + RULES[i].id);
    if (at !== null && Date.now() - Number(at) >= RULES[i].quietSeconds * 1000) due.push(RULES[i].id);
  }
  return due;
}

function dueSchedules_() {
  var slot = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH');
  var year = Number(slot.slice(0, 4)), month = Number(slot.slice(5, 7));
  var day = Number(slot.slice(8, 10)), hour = Number(slot.slice(11, 13));
  var weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();   // 0 = Sunday
  var props = PropertiesService.getScriptProperties(), due = [];
  for (var i = 0; i < RULES.length; i++) {
    var s = RULES[i].schedule;
    if (RULES[i].trigger !== 'schedule' || props.getProperty('schedule.' + RULES[i].id) === slot) continue;
    if (!inSet_(s.hours, hour) || !inSet_(s.months, month)) continue;
    var dayOk = (s.days && s.weekdays)          // cron: both restricted means either may match
      ? inSet_(s.days, day) || inSet_(s.weekdays, weekday)
      : inSet_(s.days, day) && inSet_(s.weekdays, weekday);
    if (dayOk) due.push(RULES[i].id);
  }
  return { ids: due, slot: slot };
}

function inSet_(values, value) {
  return values === null || values.indexOf(value) >= 0;   // null: any value
}
