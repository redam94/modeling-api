// Extensions > Apps Script. Store the token once under Project Settings >
// Script properties as MAPI_TOKEN.
//
// Input (optional): a sheet named "Input" in long format - a header row naming the
// endpoint's dims and input variables, then one row per coordinate - replaces the
// example's inline data. The answer's summary (mean and HDI per coordinate) is written
// to a sheet named "Result".
function scoreGeoMmmProd() {
  const token = PropertiesService.getScriptProperties().getProperty("MAPI_TOKEN");
  const payload = {
    "future_covariates": "carry_last",
    "horizon": {
      "dim": "time",
      "periods": 8
    },
    "mode": "forecast",
    "output": {
      "kind": "expected",
      "summary": [
        "mean",
        "hdi_90"
      ]
    }
  };
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const input = book.getSheetByName("Input");
  if (input) {
    const key = payload.scenario && payload.scenario.data_vars ? "scenario" : "data";
    payload[key] = mapiSheetToXr_(input.getDataRange().getValues(), payload[key]);
  }
  const response = UrlFetchApp.fetch("https://api.mapi.example/v1/endpoints/geo-mmm-prod/score", {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "Bearer " + token,
      "X-Mapi-Project": "acme/growth",
      Prefer: "wait=60",
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  if (response.getResponseCode() >= 300) {
    throw new Error(response.getResponseCode() + ": " + response.getContentText());
  }
  const result = JSON.parse(response.getContentText());
  const out = book.getSheetByName("Result") || book.insertSheet("Result");
  out.clear();
  if (!result.done) {
    const note = "Still running (" + result.run.id + "): run the function again in a minute.";
    out.getRange(1, 1).setValue(note);
    return result;
  }
  const rows = mapiSummaryRows_(result.summary);
  if (!rows.length) {
    out.getRange(1, 1).setValue(result.run.state + ": no summary in the answer");
    return result;
  }
  const width = Math.max(...rows.map((r) => r.length));
  const grid = rows.map((r) => r.concat(new Array(width - r.length).fill("")));
  out.getRange(1, 1, grid.length, width).setValues(grid);
  return result;
}

// The summary tables one below the other: a header row ("variable", the coordinate
// columns, the statistics), one row per coordinate, a blank row between tables.
function mapiSummaryRows_(summary) {
  const rows = [];
  for (const table of (summary && summary.tables) || []) {
    if (rows.length) rows.push([""]);
    rows.push(["variable"].concat(table.columns));
    for (const row of table.rows) {
      rows.push([table.variable].concat(row.map((c) => (c === null ? "" : c))));
    }
  }
  if (summary && summary.truncated) {
    rows.push(["(truncated: the full result is in the run's outputs)"]);
  }
  return rows;
}

// A long table (a header row, then rows) as an inline dataset shaped like the example's:
// columns named like its dims hold coordinates, columns named like its variables hold
// values; empty cells become null.
function mapiSheetToXr_(values, example) {
  if (!example || !example.data_vars) {
    throw new Error("this endpoint's example call sends no inline data to replace");
  }
  const header = values[0].map(String);
  const rows = values.slice(1).filter((r) => r.some((c) => c !== ""));
  const column = (name) => {
    const i = header.indexOf(name);
    if (i < 0) throw new Error('the Input sheet needs a column named "' + name + '"');
    return i;
  };
  const cell = (v) =>
    v instanceof Date ? Utilities.formatDate(v, Session.getScriptTimeZone(), "yyyy-MM-dd") : v;
  const coords = {};
  const index = {};
  for (const spec of Object.values(example.data_vars)) {
    for (const dim of spec.dims) {
      if (index[dim]) continue;
      const i = column(dim);
      index[dim] = new Map();
      const seen = [];
      for (const r of rows) {
        const key = cell(r[i]);
        if (!index[dim].has(key)) {
          index[dim].set(key, seen.length);
          seen.push(key);
        }
      }
      coords[dim] = { dims: [dim], data: seen };
    }
  }
  const data_vars = {};
  for (const [name, spec] of Object.entries(example.data_vars)) {
    const i = column(name);
    const value = (r) => (r[i] === "" ? null : r[i]);
    if (!spec.dims.length) {
      data_vars[name] = { dims: [], data: rows.length ? value(rows[0]) : null };
      continue;
    }
    const last = spec.dims.length - 1;
    const empty = (k) => coords[spec.dims[k]].data.map(() => (k < last ? empty(k + 1) : null));
    const data = empty(0);
    for (const r of rows) {
      const at = spec.dims.map((dim) => index[dim].get(cell(r[column(dim)])));
      let node = data;
      for (let k = 0; k < at.length - 1; k++) node = node[at[k]];
      node[at[at.length - 1]] = value(r);
    }
    data_vars[name] = { dims: spec.dims, data: data };
  }
  return { coords: coords, data_vars: data_vars };
}
