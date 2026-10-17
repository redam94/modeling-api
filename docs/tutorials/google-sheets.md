---
description: Connect a Google Sheet to a modeling-api endpoint with Apps Script - score it from the sheet, write a forecast with its likely range into a table, and refresh it every morning with a trigger.
---

# Connect Google Sheets <span class="mapi-badge">v0.4</span>

At the end of this tutorial, a Google Sheet holds the forecast of your model, week by week.
It shows the actual results that the model was fitted on, and the range of possible future
values. The sheet refreshes itself each morning. No person needs to open the platform. The
sheet holds only a token that can score one endpoint.

| You need | Where to get it |
|---|---|
| An **endpoint** that serves a model that forecasts (a geo marketing mix or a weekly forecast) | The Guided [*Share* step](../guides/guided-tutorial.md#7-share), or **Promote a model** in the [Integrations hub](../guides/integrations-hub.md#promoting-through-the-gate) |
| A platform that the internet can get to | Apps Script runs on the servers of Google. Thus it cannot call a stack on `127.0.0.1`. Use the deployment of your team. |
| A Google account that can use Apps Script | Any Google Workspace account or personal account |

## 1. Create a token that can only score this endpoint

1. In the web app, open **Integrations**. The **Google Sheets** card lists your endpoints.
2. Select your endpoint.
3. Click **Create a token**. The Tokens page opens with the data for a `score:<alias>` token.
   This token can read and score that endpoint and nothing else.

![The Google Sheets card: the endpoint, the four steps and the generated Apps Script](../assets/screenshots/integrations-sheets.png){ .mapi-shot style="max-width: 420px" }

Alternatively, use a terminal. This command also sets a rate limit and a monthly compute
budget:

```bash
mapi tokens create sheets-forecast -s score:geo-mmm-prod --rate-limit 60 --budget 600
```

The platform shows the secret **one time** only. All persons who can edit the script can see
the script properties of a sheet. For this reason, the token must be able to do only one
thing: score this endpoint. You can revoke the token at any time on the Tokens page
(`mapi tokens revoke`).

## 2. Paste the generated script

1. In the sheet, select **Extensions → Apps Script**.
2. Replace the empty function with the code from the card. This code is the **Apps Script**
   snippet of the endpoint. It is also on the Snippets tab of the endpoint, and you can get
   it with `mapi endpoints snippets geo-mmm-prod -l apps_script`. For an endpoint
   `geo-mmm-prod`, the code is similar to this:

    ```javascript
    --8<-- "sheets_endpoint.gs"
    ```

3. Select **Project Settings → Script properties → Add script property**.
4. Type the name `MAPI_TOKEN`. For the value, paste the token from step 1.
5. Go back to the editor and select the function.
6. Push **Run**. The first run asks you to authorise the script. The script connects to an
   external service and edits your spreadsheets.

A sheet with the name **Result** now holds the answer. The platform ran the example call of
the endpoint (here, an 8-week forecast) in a sandbox. It returned the requested summary
inline, with one table for each output variable. Each table has a header row and one row for
each coordinate. The header row contains `variable`, the coordinate columns (for example
`geo` and `time`), and then `mean`, `hdi_90_lower` and `hdi_90_upper`.

The payload is the example of the endpoint. The platform made it from the input contract of
the endpoint, so it is valid without changes. The script waits for one minute. If the run
takes longer, **Result** shows *Still running*. Then run the function again to get the
result.

You can send your own data instead of the example data. To do this, add a sheet with the
name **Input** in long format. The sheet has a header row with the dims and input variables
of the endpoint. Then it has one row for each coordinate. The script reads the sheet into the
call. This also applies to a forecast endpoint, although its example call sends no data. You
use this sheet to add your latest actuals and the dates to forecast
([step 5](#5-add-your-latest-actuals-and-future-dates)).

Here, the summary is for each coordinate, that is, for each market and week. The next step
writes a different table. That table shows the history that the model was fitted on, next to
the range of the forecast. The values are totals over all markets.

## 3. Write the forecast into a table

A forecast run also writes a small JSON table for Guided mode, `ui/guided/forecast.json`.
The table holds the history that the model was fitted on. Then it holds the quantiles of the
forecast for each period. This script gets the table and writes it into a sheet with the
name **Forecast**.

1. Replace the code with this script:

    ```javascript
    --8<-- "sheets_forecast.gs"
    ```

2. Set `MAPI_URL`, `MAPI_PROJECT`, `ENDPOINT` and `CALL`. `CALL` is the `payload` of the
   generated script.
3. Run `refreshForecast`.

The **Forecast** sheet is then similar to this table. The numbers depend on your model.

| time | Actual revenue | 5% | 25% | Median | 75% | 95% |
|---|---|---|---|---|---|---|
| 2026-09-27 | 101.5 | | | | | |
| 2026-10-04 | 98.2 | | | | | |
| 2026-10-11 | 104.9 | | | | | |
| 2026-10-18 | | 88.0 | 96.3 | 101.2 | 106.4 | 114.9 |
| 2026-10-25 | | 85.1 | 94.0 | 99.8 | 105.2 | 115.3 |
| | | | | | | |
| Forecast from 2026-10-18 | expected revenue (kEUR) | | | | | |

Read the table as follows. In 90% of plausible futures, the week of 18 October is between the
5% and 95% columns. Half of the plausible futures are between 25% and 75%. The model adds the
KPI over its other dims (markets, stores) *for each posterior draw*. Then it calculates the
quantiles. For this reason, these ranges are honest totals and not sums of the ranges for
each market. To make a chart from the table, use **Insert → Chart**. Use a line for the
median and the actuals, and an area between 5% and 95%.

The script does these things, for these reasons:

- **One run each day.** It sends an `Idempotency-Key` that contains the endpoint and the
  date. The platform keeps a key for 24 hours. If you run the script again on the same day,
  you get the same run. The platform does not start (and charge for) another run. If you
  change `CALL`, you get a `409 IDEMPOTENCY_CONFLICT` until the next day. To start again,
  rename the key.
- **Still running?** `Prefer: wait=30` waits for a maximum of 30 seconds. A cold start in the
  cloud can take longer. Then the sheet shows *Still running*. The next run of the script
  gets the result.
- **Short-lived links.** The links of the result are signed URLs that expire after 15
  minutes. The script gets the table immediately and does not store the link. The link does
  not need a token.

## 4. Refresh it every morning

1. In the Apps Script editor, select **Triggers** (the clock icon).
2. Select **Add Trigger**.
3. Select the function `refreshForecast`.
4. Select the event source **Time-driven** and **Day timer**.
5. Select a time of day.

Each morning, the trigger clears the sheet. Then it fills the sheet with a new forecast from
the version that the endpoint serves on that day. Thus, when you promote a better model, the
sheet updates. You do not have to change the sheet.

## 5. Add your latest actuals and future dates

The model was fitted one time, but each month gives a new actual. Type the actual into the
sheet, and the next forecast starts from it. The model updates its state with the actuals. It
uses the parameters from the fit and does not fit again. Then it forecasts the dates that you
list ([Forecasts with actuals](../concepts/forecasting-with-actuals.md)). This works with the
script from step 2. It works for each endpoint that has a model that can condition on
actuals. The Sheets card and `mapi endpoints schema` tell you if the model can do this. The
statsmodels time-series models can.

Add a sheet with the name **Input**, with one row for each period. The sheet has these
columns:

- The date column.
- The target. Type a value where you have an actual. **Keep the cell empty on the dates to
  forecast.**

This example is for the monthly airline endpoint `ts-forecast`. The model was fitted on
1949-01 to 1960-12, with the target `y` and no drivers:

| t | y |
|---|---|
| 1961-01-01 | 417 |
| 1961-02-01 | 391 |
| 1961-03-01 | 419 |
| 1961-04-01 | |
| 1961-05-01 | |

Run the function. The model updates with January to March 1961, and it forecasts April and
May. **Result** starts with a note in cell A1. The note tells you the data that the forecast
used. Then there is an empty row, and then the table:

```text
Forecast updated with 3 actuals (1961-01-01 to 1961-03-01) after the fitted history, which
ends 1960-12-01; the model's parameters were not re-estimated. 2 periods forecast from
1961-04-01 to 1961-05-01.

variable   t            median   q5    q95
y          1961-01-01   …        …     …
…
y          1961-05-01   …        …     …
```

The rows of the actual months hold one-step-ahead predictions. These are the values that the
model would have given in the month before. The empty months hold the forecast.

A model with drivers has a column for each driver. The model needs a driver value on each
row, also on the dates to forecast. This example is for a monthly `sales` forecast with a
`price` driver:

| month | sales | price |
|---|---|---|
| 2026-10-01 | 1520.5 | 9.99 |
| 2026-11-01 | 1610.0 | 9.99 |
| 2026-12-01 | | 10.49 |
| 2027-01-01 | | |

The price of the last month is empty. Thus `future_covariates: "carry_last"` in the script
uses the latest price that you gave again (10.49). The column names are the names of the
endpoint. You can find them in these locations:

- The **Google Sheets** card shows the layout of the selected endpoint.
- `mapi endpoints schema ALIAS` lists the columns.
- The generated script has a second function, `score<Alias>InputSheet`. This function
  creates the **Input** sheet with the correct header. It also adds the periods after the
  fit, all empty.

??? example "The generated script for the `sales-forecast` endpoint"

    ```javascript
    --8<-- "sheets_actuals.gs"
    ```

Obey these rules, so that the sheet does what you expect:

- **Dates** are cells with a date format, or text in `yyyy-MM-dd`. The script sends them as
  `yyyy-MM-dd` in the time zone of the spreadsheet. The rows can be in any sequence.
- **Headers** match without the spaces around them, and case is not important. Possibly the
  script cannot find the target column, and the sheet has columns that the script does not
  know. Then the script gives an error that lists the expected columns. It never gives a
  forecast that silently ignores your actuals.
- **Numbers stored as text** (a pasted CSV, a Plain-text column) go as numbers. A cell that
  holds only spaces is empty. The platform refuses other text in a number column (`#N/A`)
  with `SCHEMA_INVALID`. The error names the column and the date.
- **No rows** (only the header): the model forecasts the 8 periods after the fit, as without
  a sheet.
- **An empty target after the last actual** is a date to forecast. If the sheet ends with an
  actual, the model forecasts the 8 periods after it (the `horizon.periods` of the call).
- **An empty target between actuals** is a gap. The refusal `ACTUALS_INVALID` tells you this.
  This is not the case if the model accepts missing observations. The state-space models
  accept them and filter them as missing.
- **Actuals inside the fitted range** replace the fitted values (a revised figure). The
  parameters stay as fitted. Fit the model again from time to time, so that the parameters
  learn from the new data.
- **A model that cannot use actuals** refuses the sheet with `ACTUALS_NOT_SUPPORTED`. It does
  not ignore the numbers. To forecast from the end of its fit, keep the target empty.
  Alternatively, fit the model again. The script writes the refusal and the corrective
  action into **Result**.

## When something goes wrong

| What the sheet or the editor shows | Why | What to do |
|---|---|---|
| `401: …` | There is no token, or the token is revoked or expired. | Examine the `MAPI_TOKEN` script property. Create a new token. |
| `403: …` | The scope of the token is not `score:<this alias>`, or the project header is not correct. | Create the token from the Sheets card. Examine `MAPI_PROJECT`. |
| `404: …` | A person deleted or renamed the endpoint. | Select the endpoint again in the Sheets card. |
| `409: …` with `IDEMPOTENCY_CONFLICT` | `CALL` changed after the first run of the day, with the same key. | Wait until the next day, or change the prefix of the key. |
| `422: …` with `MISSING_FUTURE_COVARIATE`, or `409: …` with `HORIZON_EXCEEDS_FIT` | The call asks for something that the model cannot answer. Possibly it asks for future values that the model does not have, or for more periods than the model can forecast. | Copy the payload from the snippet of the endpoint again. Make the horizon shorter. |
| `422: …` with `ACTUALS_NOT_SUPPORTED` (also in **Result**) | The **Input** sheet has target values, and the model of the endpoint cannot update a forecast with them. | To forecast from the end of the fit, keep the target empty. Alternatively, fit the model again with the new data. |
| `422: …` with `ACTUALS_INVALID` | There is an empty target between two actuals. Alternatively, the actuals do not continue the fitted history, or a row with an actual has no driver value. | Fill the gap, or remove the rows after it. Start immediately after the fit. Fill the driver. |
| `422: …` with `SCHEMA_INVALID` that names a column | A target or driver cell holds text that is not a number (`#N/A`, `n/a`). | Clear the cell, or type the number in it. |
| `failed: no summary in the answer` in **Result**, and then the problem | The API accepted the call, but the run failed. For inline data, only the run can do some checks. For example, one series of a panel has no driver on an actual month. (The API check sees all series together.) | Read the problem line below it. Correct the rows that it names. |
| `the Input sheet has no column for y (the actuals) but has "sales"` | The header of the target column is not the name that the endpoint uses. | Rename the column to the listed name. Copy the header from the Sheets card. |
| `the Input sheet needs a column named "t"` | The header of the **Input** sheet does not name the columns of the endpoint. | Copy the header from the Sheets card. Alternatively, run `score<Alias>InputSheet` on a sheet without **Input**. |
| `429: …` with `RATE_LIMITED` or `QUOTA_EXCEEDED` | The rate limit of the token, its budget or a project quota. | Increase the limit or the budget on the Tokens page, or refresh less frequently. |
| *This endpoint's model wrote no forecast table.* | The call is not a forecast, or the outputs of the model have no KPI for a table. | Use a forecast call. For other outputs, read the result with the Python client. |

## Next

- [Embed a chart](embed-a-chart.md) in a doc or a dashboard, next to the sheet.
- [Receive webhooks](../guides/webhook-receivers.md) to refresh the sheet immediately when a
  better model goes live. Then you do not need a timer.
- The [Integrations hub](../guides/integrations-hub.md) gives all the snippet and token
  options.
