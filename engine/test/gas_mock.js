/**
 * In-memory mock of the Apps Script services used by Engine.gs and legacy.gs.
 *
 * Evaluated INSIDE the same vm context as the script under test (so Date objects
 * and instanceof checks line up). Node helpers arrive via the `__node` global.
 *
 * Fidelity notes (behaviours the engine relies on, mirrored from Sheets):
 *  - getLastRow/getLastColumn = last row/column holding a value (0 when empty)
 *  - getValues returns '' for empty cells and Date objects for dates
 *  - getFontColors -> '#000000' by default, getBackgrounds -> '#ffffff' for no fill,
 *    getFontLines -> 'none'
 *  - setValues/setFontColors/... require exact array dimensions
 *  - ranges beyond getMaxRows()/getMaxColumns() throw
 *  - deleteRows/insertRowsBefore shift values, formats and validations
 *  - script properties reject values larger than 9 KB
 *  - edits made by the script do not fire onEdit (only harness.edit() does)
 */
(function (global) {
  'use strict';
  var node = global.__node;
  var calls = { reads: 0, writes: 0, structural: 0, byMethod: {} };
  var failures = [];  // [{method, after}] -> throw on the Nth call of method

  function count(kind, method) {
    calls[kind]++;
    calls.byMethod[method] = (calls.byMethod[method] || 0) + 1;
    failures.forEach(function (f) {
      if (f.method === method) {
        f.after--;
        if (f.after < 0 && !f.fired) {
          f.fired = true;
          throw new Error('injected failure in ' + method);
        }
      }
    });
  }

  function isDate(v) { return Object.prototype.toString.call(v) === '[object Date]'; }
  function cloneValue(v) { return isDate(v) ? new Date(v.getTime()) : v; }

  function newCell() {
    return { v: '', fc: null, fl: 'none', bg: null, ff: null, fs: null, fw: null, ha: null, va: null,
             wr: null, nf: null, dv: null };
  }
  var FORMAT_KEYS = ['fc', 'fl', 'bg', 'ff', 'fs', 'fw', 'ha', 'va', 'wr', 'nf'];

  /* ------------------------------------------------------------------ Sheet */

  function Sheet(ss, name) {
    this._ss = ss;
    this._name = name;
    this._rows = [];          // sparse: _rows[r][c] = cell (0-based)
    this._maxRows = 1000;
    this._maxCols = 26;
    this._merges = [];
    this._bandings = [];
    this._borders = [];
    this._protections = [];
    this._frozenRows = 0;
    this._rowHeights = {};
    this._colWidths = {};
    this._hidden = false;
    this._id = ss._nextSheetId++;
  }
  Sheet.prototype._cell = function (r, c, create) {
    var row = this._rows[r];
    if (!row) { if (!create) return null; row = this._rows[r] = []; }
    var cell = row[c];
    if (!cell && create) cell = row[c] = newCell();
    return cell || null;
  };
  Sheet.prototype.getName = function () { return this._name; };
  Sheet.prototype.getSheetId = function () { return this._id; };
  Sheet.prototype.getParent = function () { return this._ss; };
  Sheet.prototype.getMaxRows = function () { return this._maxRows; };
  Sheet.prototype.getMaxColumns = function () { return this._maxCols; };
  Sheet.prototype.getLastRow = function () {
    for (var r = this._rows.length - 1; r >= 0; r--) {
      var row = this._rows[r];
      if (row && row.some(function (c) { return c && c.v !== '' && c.v !== null && c.v !== undefined; })) return r + 1;
    }
    return 0;
  };
  Sheet.prototype.getLastColumn = function () {
    var last = 0;
    this._rows.forEach(function (row) {
      if (!row) return;
      row.forEach(function (c, i) { if (c && c.v !== '' && c.v !== null && c.v !== undefined && i + 1 > last) last = i + 1; });
    });
    return last;
  };
  Sheet.prototype.getRange = function (row, col, numRows, numCols) {
    if (typeof row === 'string') throw new Error('A1 notation not supported by mock');
    return new Range(this, row, col, numRows === undefined ? 1 : numRows, numCols === undefined ? 1 : numCols);
  };
  Sheet.prototype.getDataRange = function () {
    return new Range(this, 1, 1, Math.max(this.getLastRow(), 1), Math.max(this.getLastColumn(), 1));
  };
  Sheet.prototype.clear = function () {
    count('writes', 'Sheet.clear');
    this._rows = [];
    this._borders = [];  // clear() removes formatting, borders included
    return this;
  };
  Sheet.prototype.clearContents = function () {
    count('writes', 'Sheet.clearContents');
    this._rows.forEach(function (row) { if (row) row.forEach(function (c) { if (c) c.v = ''; }); });
    return this;
  };
  Sheet.prototype.deleteRows = function (start, n) {
    count('structural', 'Sheet.deleteRows');
    if (start < 1 || start + n - 1 > this._maxRows) throw new Error('deleteRows out of bounds');
    if (n >= this._maxRows) throw new Error('cannot delete all rows');
    this._rows.splice(start - 1, n);
    this._maxRows -= n;
    this._merges = this._merges.filter(function (m) { return m.row < start || m.row > start + n - 1; });
  };
  Sheet.prototype.deleteRow = function (r) { this.deleteRows(r, 1); };
  Sheet.prototype.insertRowsBefore = function (before, n) {
    count('structural', 'Sheet.insertRowsBefore');
    if (before < 1 || before > this._maxRows) throw new Error('insertRowsBefore out of bounds');
    var blanks = [];
    for (var i = 0; i < n; i++) blanks.push(undefined);
    while (this._rows.length < before - 1) this._rows.push(undefined);
    Array.prototype.splice.apply(this._rows, [before - 1, 0].concat(blanks));
    this._maxRows += n;
  };
  Sheet.prototype.insertRowsAfter = function (after, n) {
    count('structural', 'Sheet.insertRowsAfter');
    if (after < 1 || after > this._maxRows) throw new Error('insertRowsAfter out of bounds');
    if (after < this._rows.length) {
      var blanks = [];
      for (var i = 0; i < n; i++) blanks.push(undefined);
      Array.prototype.splice.apply(this._rows, [after, 0].concat(blanks));
    }
    this._maxRows += n;
  };
  Sheet.prototype.insertColumnsAfter = function (after, n) {
    count('structural', 'Sheet.insertColumnsAfter');
    this._maxCols += n;
  };
  Sheet.prototype.setRowHeight = function (r, h) { this._rowHeights[r] = h; return this; };
  Sheet.prototype.setColumnWidth = function (c, w) { count('writes', 'Sheet.setColumnWidth'); this._colWidths[c] = w; return this; };
  Sheet.prototype.getColumnWidth = function (c) { return this._colWidths[c] || 100; };
  Sheet.prototype.setFrozenRows = function (n) { this._frozenRows = n; };
  Sheet.prototype.getFrozenRows = function () { return this._frozenRows; };
  Sheet.prototype.getBandings = function () {
    var self = this;
    return this._bandings.map(function (b) {
      return { remove: function () { self._bandings = self._bandings.filter(function (x) { return x !== b; }); } };
    });
  };
  Sheet.prototype.getCharts = function () { return []; };
  Sheet.prototype.removeChart = function () {};
  Sheet.prototype.hideSheet = function () { this._hidden = true; return this; };
  Sheet.prototype.isSheetHidden = function () { return this._hidden; };
  Sheet.prototype.protect = function () {
    var self = this;
    var p = {
      _desc: '', _domain: true,
      setDescription: function (d) { p._desc = d; return p; },
      getDescription: function () { return p._desc; },
      getEditors: function () { return [{ getEmail: function () { return 'collab@example.com'; } }]; },
      removeEditors: function () { return p; },
      canDomainEdit: function () { return p._domain; },
      setDomainEdit: function (v) { p._domain = v; return p; },
      remove: function () { self._protections = self._protections.filter(function (x) { return x !== p; }); }
    };
    this._protections.push(p);
    return p;
  };
  Sheet.prototype.getProtections = function () { return this._protections.slice(); };

  /* ------------------------------------------------------------------ Range */

  function Range(sheet, row, col, nr, nc) {
    if (row < 1 || col < 1 || nr < 1 || nc < 1) {
      throw new Error('Range out of bounds: ' + [row, col, nr, nc].join(','));
    }
    if (row + nr - 1 > sheet._maxRows || col + nc - 1 > sheet._maxCols) {
      throw new Error('Range exceeds grid (' + sheet._name + ' ' + [row, col, nr, nc].join(',') +
        ' max ' + sheet._maxRows + 'x' + sheet._maxCols + ')');
    }
    this._s = sheet; this._r = row; this._c = col; this._nr = nr; this._nc = nc;
  }
  Range.prototype.getSheet = function () { return this._s; };
  Range.prototype.getRow = function () { return this._r; };
  Range.prototype.getColumn = function () { return this._c; };
  Range.prototype.getLastRow = function () { return this._r + this._nr - 1; };
  Range.prototype.getLastColumn = function () { return this._c + this._nc - 1; };
  Range.prototype.getNumRows = function () { return this._nr; };
  Range.prototype.getNumColumns = function () { return this._nc; };
  Range.prototype._read = function (fn, method) {
    count('reads', method);
    var out = [];
    for (var i = 0; i < this._nr; i++) {
      var row = [];
      for (var j = 0; j < this._nc; j++) row.push(fn(this._s._cell(this._r - 1 + i, this._c - 1 + j, false)));
      out.push(row);
    }
    return out;
  };
  Range.prototype._write = function (arr, fn, method) {
    count('writes', method);
    if (!Array.isArray(arr) || arr.length !== this._nr) {
      throw new Error(method + ': expected ' + this._nr + ' rows, got ' + (arr && arr.length));
    }
    for (var i = 0; i < this._nr; i++) {
      if (!Array.isArray(arr[i]) || arr[i].length !== this._nc) {
        throw new Error(method + ': row ' + i + ' expected ' + this._nc + ' columns, got ' + (arr[i] && arr[i].length));
      }
    }
    for (var a = 0; a < this._nr; a++) {
      for (var b = 0; b < this._nc; b++) fn(this._s._cell(this._r - 1 + a, this._c - 1 + b, true), arr[a][b]);
    }
    return this;
  };
  Range.prototype._fill = function (value, fn, method) {
    var arr = [];
    for (var i = 0; i < this._nr; i++) { var row = []; for (var j = 0; j < this._nc; j++) row.push(value); arr.push(row); }
    return this._write(arr, fn, method);
  };
  Range.prototype.getValues = function () {
    return this._read(function (c) { return c ? cloneValue(c.v === null || c.v === undefined ? '' : c.v) : ''; }, 'Range.getValues');
  };
  Range.prototype.getValue = function () { return this.getValues()[0][0]; };
  Range.prototype.setValues = function (arr) {
    return this._write(arr, function (c, v) {
      if (v !== null && typeof v === 'object' && !isDate(v)) throw new Error('setValues: unsupported object value');
      c.v = v === null || v === undefined ? '' : cloneValue(v);
    }, 'Range.setValues');
  };
  Range.prototype.setValue = function (v) { return this._fill(v, function (c, x) { c.v = cloneValue(x); }, 'Range.setValue'); };
  Range.prototype.clearContent = function () { return this._fill('', function (c) { c.v = ''; }, 'Range.clearContent'); };
  Range.prototype.getFontColors = function () {
    return this._read(function (c) { return c && c.fc ? c.fc : '#000000'; }, 'Range.getFontColors');
  };
  Range.prototype.setFontColors = function (arr) {
    return this._write(arr, function (c, v) { c.fc = v ? String(v).toLowerCase() : null; }, 'Range.setFontColors');
  };
  Range.prototype.setFontColor = function (v) {
    return this._fill(v, function (c, x) { c.fc = x ? String(x).toLowerCase() : null; }, 'Range.setFontColor');
  };
  Range.prototype.getFontLines = function () {
    return this._read(function (c) { return c && c.fl ? c.fl : 'none'; }, 'Range.getFontLines');
  };
  Range.prototype.setFontLines = function (arr) {
    return this._write(arr, function (c, v) {
      if (v !== null && ['none', 'underline', 'line-through'].indexOf(v) === -1) throw new Error('bad font line ' + v);
      c.fl = v || 'none';
    }, 'Range.setFontLines');
  };
  Range.prototype.getBackgrounds = function () {
    return this._read(function (c) { return c && c.bg ? c.bg : '#ffffff'; }, 'Range.getBackgrounds');
  };
  Range.prototype.setBackgrounds = function (arr) {
    return this._write(arr, function (c, v) { c.bg = v ? String(v).toLowerCase() : null; }, 'Range.setBackgrounds');
  };
  Range.prototype.setBackground = function (v) {
    return this._fill(v, function (c, x) { c.bg = x ? String(x).toLowerCase() : null; }, 'Range.setBackground');
  };
  function simpleSetter(key, method) {
    return function (v) { return this._fill(v, function (c, x) { c[key] = x; }, method); };
  }
  Range.prototype.setFontFamily = simpleSetter('ff', 'Range.setFontFamily');
  Range.prototype.setFontSize = simpleSetter('fs', 'Range.setFontSize');
  Range.prototype.setFontWeight = simpleSetter('fw', 'Range.setFontWeight');
  Range.prototype.setHorizontalAlignment = simpleSetter('ha', 'Range.setHorizontalAlignment');
  Range.prototype.setVerticalAlignment = simpleSetter('va', 'Range.setVerticalAlignment');
  Range.prototype.setWrap = simpleSetter('wr', 'Range.setWrap');
  Range.prototype.setNumberFormat = simpleSetter('nf', 'Range.setNumberFormat');
  Range.prototype.getNumberFormat = function () {
    var c = this._s._cell(this._r - 1, this._c - 1, false);
    count('reads', 'Range.getNumberFormat');
    return c && c.nf ? c.nf : 'General';
  };
  Range.prototype.setDataValidation = function (rule) {
    return this._fill(rule, function (c, x) { c.dv = x ? { values: x.values.slice(), allowInvalid: x.allowInvalid } : null; },
      'Range.setDataValidation');
  };
  Range.prototype.getDataValidations = function () {
    return this._read(function (c) { return c ? c.dv : null; }, 'Range.getDataValidations');
  };
  Range.prototype.merge = function () {
    count('writes', 'Range.merge');
    this._s._merges.push({ row: this._r, col: this._c, nr: this._nr, nc: this._nc });
    return this;
  };
  Range.prototype.breakApart = function () {
    count('writes', 'Range.breakApart');
    var r = this;
    this._s._merges = this._s._merges.filter(function (m) {
      return m.row + m.nr - 1 < r._r || m.row > r.getLastRow() || m.col + m.nc - 1 < r._c || m.col > r.getLastColumn();
    });
    return this;
  };
  Range.prototype.applyRowBanding = function (theme, header, footer) {
    count('writes', 'Range.applyRowBanding');
    if (!theme) throw new Error('applyRowBanding: theme required');
    this._s._bandings.push({ row: this._r, col: this._c, nr: this._nr, nc: this._nc, theme: theme, header: header, footer: footer });
    return {};
  };
  Range.prototype.setBorder = function () {
    count('writes', 'Range.setBorder');
    this._s._borders.push({ row: this._r, col: this._c, nr: this._nr, nc: this._nc, args: Array.prototype.slice.call(arguments, 0, 7) });
    return this;
  };

  /* ------------------------------------------------------------ Spreadsheet */

  function Spreadsheet() {
    this._sheets = [];
    this._nextSheetId = 1;
    this.toasts = [];
    this.alerts = [];
    this.menus = [];
  }
  Spreadsheet.prototype.getId = function () { return 'mock-spreadsheet-id'; };
  Spreadsheet.prototype.getSheets = function () { return this._sheets.slice(); };
  Spreadsheet.prototype.getSheetByName = function (n) {
    for (var i = 0; i < this._sheets.length; i++) if (this._sheets[i]._name === n) return this._sheets[i];
    return null;
  };
  Spreadsheet.prototype.insertSheet = function (name, index) {
    count('structural', 'Spreadsheet.insertSheet');
    if (this.getSheetByName(name)) throw new Error('sheet exists: ' + name);
    var sh = new Sheet(this, name);
    if (index === undefined) this._sheets.push(sh); else this._sheets.splice(index, 0, sh);
    return sh;
  };
  Spreadsheet.prototype.deleteSheet = function (sh) { this._sheets = this._sheets.filter(function (s) { return s !== sh; }); };
  Spreadsheet.prototype.getActiveSheet = function () { return this._sheets[0]; };
  Spreadsheet.prototype.toast = function (msg, title) { this.toasts.push({ msg: msg, title: title }); };
  Spreadsheet.prototype.getSpreadsheetTimeZone = function () { return global.__tz; };

  var SS = new Spreadsheet();

  var ui = {
    Button: { OK: 'OK', CANCEL: 'CANCEL' },
    ButtonSet: { OK: 'OK', OK_CANCEL: 'OK_CANCEL' },
    _prompts: [],
    createMenu: function (title) {
      var menu = { title: title, items: [] };
      var api = {
        addItem: function (label, fn) { menu.items.push([label, fn]); return api; },
        addSeparator: function () { return api; },
        addToUi: function () { SS.menus.push(menu); }
      };
      return api;
    },
    alert: function () { SS.alerts.push(Array.prototype.slice.call(arguments)); return 'OK'; },
    prompt: function () {
      var next = ui._prompts.shift();
      return { getSelectedButton: function () { return next === undefined ? 'CANCEL' : 'OK'; },
               getResponseText: function () { return next || ''; } };
    }
  };

  global.SpreadsheetApp = {
    getActive: function () { return SS; },
    getActiveSpreadsheet: function () { return SS; },
    getActiveSheet: function () { return SS._sheets[0]; },
    getUi: function () { return ui; },
    flush: function () {},
    newDataValidation: function () {
      var rule = { values: null, allowInvalid: true };
      var b = {
        requireValueInList: function (values) { rule.values = values.slice(); return b; },
        setAllowInvalid: function (v) { rule.allowInvalid = v; return b; },
        build: function () { return { values: rule.values, allowInvalid: rule.allowInvalid }; }
      };
      return b;
    },
    BandingTheme: { LIGHT_GREY: 'LIGHT_GREY', CYAN: 'CYAN', GREEN: 'GREEN', YELLOW: 'YELLOW', ORANGE: 'ORANGE',
                    BLUE: 'BLUE', TEAL: 'TEAL', GREY: 'GREY', BROWN: 'BROWN', LIGHT_GREEN: 'LIGHT_GREEN',
                    INDIGO: 'INDIGO', PINK: 'PINK' },
    BorderStyle: { SOLID: 'SOLID' },
    ProtectionType: { SHEET: 'SHEET', RANGE: 'RANGE' }
  };

  /* ------------------------------------------------------------- Properties */

  var store = {};
  var PROP_LIMIT = 9 * 1024;
  function checkSize(key, value) {
    if (node.byteLength(String(key)) + node.byteLength(String(value)) > PROP_LIMIT) {
      throw new Error('Property value too large for key ' + key);
    }
  }
  var scriptProps = {
    getProperty: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setProperty: function (k, v) {
      if (global.__propsDown) throw new Error('PropertiesService unavailable');
      checkSize(k, v); store[k] = String(v); return scriptProps;
    },
    setProperties: function (map, deleteOthers) {
      if (global.__propsDown) throw new Error('PropertiesService unavailable');
      Object.keys(map).forEach(function (k) { checkSize(k, map[k]); });
      if (deleteOthers) store = {};
      Object.keys(map).forEach(function (k) { store[k] = String(map[k]); });
      return scriptProps;
    },
    deleteProperty: function (k) { delete store[k]; return scriptProps; },
    getProperties: function () { return Object.assign({}, store); },
    getKeys: function () { return Object.keys(store); }
  };
  global.PropertiesService = { getScriptProperties: function () { return scriptProps; } };

  global.LockService = {
    getScriptLock: function () {
      return {
        tryLock: function () { return !global.__lockBusy; },
        waitLock: function () { if (global.__lockBusy) throw new Error('Lock timeout'); },
        releaseLock: function () {},
        hasLock: function () { return true; }
      };
    }
  };

  /* -------------------------------------------------------------- Utilities */

  function signedBytes(buf) { return Array.prototype.map.call(buf, function (b) { return b > 127 ? b - 256 : b; }); }
  function Blob(bytes, contentType, name) { this._buf = bytes; this._type = contentType; this._name = name; }
  Blob.prototype.getBytes = function () { return signedBytes(this._buf); };
  Blob.prototype.getDataAsString = function () { return node.bufToString(this._buf); };
  Blob.prototype.getContentType = function () { return this._type; };
  Blob.prototype.setName = function (n) { this._name = n; return this; };

  global.Utilities = {
    DigestAlgorithm: { SHA_256: 'sha256' },
    Charset: { UTF_8: 'utf8' },
    computeDigest: function (alg, text) { return signedBytes(node.sha256(text)); },
    getUuid: function () { return node.uuid(); },
    newBlob: function (data, contentType, name) {
      return new Blob(typeof data === 'string' ? node.stringToBuf(data) : node.bytesToBuf(data), contentType, name);
    },
    gzip: function (blob) { return new Blob(node.gzip(blob._buf), 'application/x-gzip', blob._name); },
    ungzip: function (blob) { return new Blob(node.gunzip(blob._buf), 'application/octet-stream', blob._name); },
    base64Encode: function (bytes) { return node.base64Encode(bytes); },
    base64Decode: function (text) { return signedBytes(node.base64Decode(text)); },
    formatDate: function (date, tz, fmt) { return node.formatDate(date.getTime(), fmt); },
    sleep: function () {}
  };

  global.Session = {
    getScriptTimeZone: function () { return global.__tz; },
    getActiveUser: function () { return { getEmail: function () { return 'owner@example.com'; } }; },
    getEffectiveUser: function () { return { getEmail: function () { return 'owner@example.com'; } }; }
  };

  /* --------------------------------------------------------------- ScriptApp */

  var triggers = [];
  function trigger(fn, kind, extra) {
    var t = { fn: fn, kind: kind, extra: extra || {}, id: 't' + (triggers.length + 1) };
    t.getHandlerFunction = function () { return fn; };
    t.getUniqueId = function () { return t.id; };
    t.getEventType = function () { return kind; };
    return t;
  }
  global.ScriptApp = {
    getProjectTriggers: function () { return triggers.slice(); },
    deleteTrigger: function (t) { triggers = triggers.filter(function (x) { return x !== t; }); },
    newTrigger: function (fn) {
      return {
        forSpreadsheet: function () {
          return {
            onEdit: function () { return { create: function () { var t = trigger(fn, 'ON_EDIT'); triggers.push(t); return t; } }; },
            onOpen: function () { return { create: function () { var t = trigger(fn, 'ON_OPEN'); triggers.push(t); return t; } }; },
            onChange: function () { return { create: function () { var t = trigger(fn, 'ON_CHANGE'); triggers.push(t); return t; } }; }
          };
        },
        timeBased: function () {
          var spec = {};
          var b = {
            everyMinutes: function (n) { spec.minutes = n; return b; },
            everyHours: function (n) { spec.hours = n; return b; },
            create: function () { var t = trigger(fn, 'CLOCK', spec); triggers.push(t); return t; }
          };
          return b;
        }
      };
    },
    getScriptId: function () { return 'mock-script-id'; }
  };

  /* ------------------------------------------------------------- UrlFetchApp */

  var fetches = [];
  global.UrlFetchApp = {
    fetch: function (url, opts) {
      fetches.push({ url: url, opts: opts, payload: opts && opts.payload ? JSON.parse(opts.payload) : null });
      if (global.__fetchMode === 'throw') throw new Error('DNS error: ' + url);
      var code = global.__fetchMode === 'http500' ? 500 : 200;
      return { getResponseCode: function () { return code; }, getContentText: function () { return '{}'; } };
    }
  };

  /* ------------------------------------------------------------ test hooks */

  global.__mock = {
    ss: SS,
    ui: ui,
    calls: calls,
    fetches: fetches,
    props: function () { return store; },
    triggers: function () { return triggers; },
    failOn: function (method, after) { failures.push({ method: method, after: after || 0, fired: false }); },
    resetCalls: function () {
      calls.reads = 0; calls.writes = 0; calls.structural = 0; calls.byMethod = {};
    },
    loadWorkbook: function (wb) {
      wb.tabs.forEach(function (t) {
        var sh = SS.insertSheet(t.name);
        var width = 0;
        t.rows.forEach(function (row) { if (row.length > width) width = row.length; });
        if (width > sh._maxCols) sh._maxCols = width;
        if (t.rows.length > sh._maxRows) sh._maxRows = t.rows.length + 10;
        t.rows.forEach(function (row, r) {
          row.forEach(function (v, c) {
            var decoded = global.__decode(v);
            if (decoded === '' || decoded === null) return;
            sh._cell(r, c, true).v = decoded;
          });
        });
        (t.number_formats || []).forEach(function (nf) {
          for (var r = nf.row; r < nf.row + nf.rows; r++) sh._cell(r - 1, nf.col - 1, true).nf = nf.format;
        });
        (t.formats || []).forEach(function (f) {
          var cell = sh._cell(f.row - 1, f.col - 1, true);
          if (f.font !== undefined) cell.fc = f.font ? f.font.toLowerCase() : null;
          if (f.background !== undefined) cell.bg = f.background ? f.background.toLowerCase() : null;
          if (f.strike !== undefined) cell.fl = f.strike ? 'line-through' : 'none';
        });
      });
    },
    edit: function (tab, row, col, value) {
      var sh = SS.getSheetByName(tab);
      var rng = sh.getRange(row, col);
      sh._cell(row - 1, col - 1, true).v = global.__decode(value);
      return global.__mock.fire('ON_EDIT', { range: rng, source: SS, value: value, authMode: 'FULL', user: 'collab@example.com' });
    },
    fire: function (kind, event) {
      var ran = [];
      triggers.filter(function (t) { return t.kind === kind; }).forEach(function (t) {
        if (typeof global[t.fn] !== 'function') throw new Error('trigger handler missing: ' + t.fn);
        ran.push(t.fn);
        global[t.fn](event || {});
      });
      return ran;
    },
    dump: function (options) {
      options = options || {};
      return SS._sheets.map(function (sh) {
        var h = Math.max(sh.getLastRow(), options.minRows || 0);
        var w = Math.max(sh.getLastColumn(), options.minCols || 0);
        var cells = [];
        for (var r = 0; r < h; r++) {
          var row = [];
          for (var c = 0; c < w; c++) {
            var cell = sh._cell(r, c, false) || newCell();
            var out = { v: global.__encode(cell.v === undefined || cell.v === null ? '' : cell.v) };
            FORMAT_KEYS.forEach(function (k) { out[k] = cell[k]; });
            out.fc = out.fc || '#000000';
            out.bg = out.bg && out.bg !== '#ffffff' ? out.bg : null;
            out.dv = cell.dv;
            row.push(out);
          }
          cells.push(row);
        }
        return {
          name: sh._name, hidden: sh._hidden, maxRows: sh._maxRows, lastRow: sh.getLastRow(), lastColumn: w,
          cells: cells, merges: sh._merges, bandings: sh._bandings, borders: sh._borders,
          protected: sh._protections.length > 0, frozenRows: sh._frozenRows,
          rowHeights: sh._rowHeights, colWidths: sh._colWidths
        };
      });
    }
  };
})(globalThis);
