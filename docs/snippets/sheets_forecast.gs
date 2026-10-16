// A forecast from a modeling-api endpoint, written into the sheet "Forecast": the weeks the
// model was fitted on (with your actual results), then the forecast with its likely range.
// Extensions > Apps Script: paste this file. Project Settings > Script properties: add
// MAPI_TOKEN (a token that can only score this endpoint).
const MAPI_URL = "https://api.mapi.example"; // your platform's API address
const MAPI_PROJECT = "acme/growth"; // <tenant>/<project>
const ENDPOINT = "geo-mmm-prod";
// The call: the "payload" of the hub's Apps Script snippet for your endpoint.
const CALL = {
  mode: "forecast",
  horizon: { dim: "time", periods: 8 },
  future_covariates: "carry_last",
  output: { kind: "expected", summary: ["mean", "hdi_90"] },
};

function refreshForecast() {
  const token = PropertiesService.getScriptProperties().getProperty("MAPI_TOKEN");
  // One score run per day: running this again the same day collects that run's result
  // instead of starting another (the platform remembers an Idempotency-Key for 24 hours).
  const day = Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd");
  const response = UrlFetchApp.fetch(`${MAPI_URL}/v1/endpoints/${ENDPOINT}/score`, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "Bearer " + token,
      "X-Mapi-Project": MAPI_PROJECT,
      Prefer: "wait=30",
      "Idempotency-Key": `sheets-${ENDPOINT}-${day}`,
    },
    payload: JSON.stringify(CALL),
    muteHttpExceptions: true,
  });
  if (response.getResponseCode() >= 300 && response.getResponseCode() !== 202) {
    throw new Error(response.getResponseCode() + ": " + response.getContentText());
  }
  const result = JSON.parse(response.getContentText());
  const sheet = forecastSheet();
  if (!result.done) {
    sheet.getRange("A1").setValue(`Still running (${result.run.state}): run this again in a minute.`);
    return;
  }
  if (result.run.state !== "succeeded") {
    sheet.getRange("A1").setValue(`The forecast failed: ${result.run.state}. See run ${result.run.id}.`);
    return;
  }
  const link = result.outputs.find((o) => o.name === "ui/guided/forecast.json");
  if (!link) {
    sheet.getRange("A1").setValue("This endpoint's model wrote no forecast table.");
    return;
  }
  // A short-lived signed link: fetch it now, never store it. It needs no token.
  const view = JSON.parse(UrlFetchApp.fetch(link.url).getContentText());
  const fan = view.fan;
  const header = [fan.dim, `Actual ${view.kpi}`].concat(
    fan.quantiles.map((q) => (q === 0.5 ? "Median" : `${Math.round(q * 100)}%`)),
  );
  const rows = fan.coords.map((coord, j) =>
    [coord, cell(fan.observed && fan.observed[j])].concat(fan.values.map((row) => cell(row[j]))),
  );
  sheet.getRange(1, 1, 1, header.length).setValues([header]);
  sheet.getRange(2, 1, rows.length, header.length).setValues(rows);
  const units = view.units ? ` (${view.units})` : "";
  sheet
    .getRange(rows.length + 3, 1, 1, 2)
    .setValues([[`Forecast from ${view.split_at}`, `${view.kind} ${view.kpi}${units}`]]);
}

// An empty cell for a missing value (no actuals after the split, no forecast before it).
function cell(value) {
  return value === null || value === undefined ? "" : value;
}

function forecastSheet() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = book.getSheetByName("Forecast") || book.insertSheet("Forecast");
  sheet.clearContents();
  return sheet;
}
