var PRODUCT = '${product}';
var GENERATOR = '${generator}';
var CONFIG_VERSION = ${config_version};

// The row your column names are on, and the first row of data below it.
var HEADER_ROW = ${header_row};
var DATA_START_ROW = ${data_start_row};

// Rows with this column set are left alone by every rule.
var HOLD_COLUMN = '${hold_column}';

// A run that would move more rows than this stops instead of reorganising half the sheet.
var MAX_ROWS_PER_RUN = ${max_rows};

// Rows removed or overwritten are copied to the hidden _backup tab; this many runs are kept.
var BACKUP_RUNS = ${backup_runs};

// The tabs this script governs, with the fingerprint of each header row at generation time.
var GOVERNED_TABS = [
${governed}
];
