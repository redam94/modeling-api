// Extensions > Apps Script. Store the token once under Project Settings >
// Script properties as MAPI_TOKEN.
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
  SpreadsheetApp.getActiveSheet().getRange("A1").setValue(result.done ? "done" : result.run.state);
  return result;
}
