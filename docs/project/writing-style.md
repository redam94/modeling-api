# Writing style: Simplified Technical English

All text for users in this project is in ASD-STE100 Simplified Technical English (STE).
STE makes instructions clear and short. It also makes them easy to read for persons who do not
speak English as a first language, and easy to translate.

This page gives the rules that we use. It does not replace the ASD-STE100 specification. When
you are not sure, read the specification (Issue 9), section 1 (words) and section 2 (writing
rules).

## Where these rules apply

| Text | Rules |
|---|---|
| Documentation (`docs/`) | Yes, all pages. Procedures and descriptions. |
| Text in the web app | Yes: labels, buttons, hints, help text, empty states, errors and tooltips. |
| Text that the server sends for the app to show | Yes: About cards, scorecard messages, suggested actions and plain-language error messages. |
| New CHANGELOG entries | Yes. Older entries stay as they are. |
| Code, identifiers, API field names and log text for developers | No. Keep the names that the code uses. |
| Code comments, docstrings and commit messages | No. |

## Words

1. Use approved words from the STE dictionary. Use each word only with its approved meaning
   and its approved part of speech. For example, "test" is a noun and a verb, but "check" is
   a noun only. Use "do a check of" or "make sure that".
2. You can use technical names and technical verbs (see the
   [project word list](#project-technical-names-and-verbs)). Use a technical name only as a
   noun or an adjective.
3. Use one word for one thing. Do not use synonyms. For example, always write "dataset", not
   "data set", "table" or "file" for the same thing.
4. Do not use the "-ing" form of a verb as a noun or as an adjective, except in a technical
   name. Write "when you score a model", not "when scoring a model".
5. Use "the", "a" or "an" before nouns where possible.
6. Do not make noun clusters of more than three words. Write "the input table of the model",
   not "model input table layout".

### Words to replace

| Do not write | Write |
|---|---|
| ensure | make sure |
| utilize, leverage | use |
| perform, carry out | do |
| obtain, retrieve | get |
| require, requirement | need, necessary |
| prior to | before |
| in order to | to |
| via | through, with |
| approximately | about |
| assist | help |
| may (possibility) | can, possibly |
| e.g. | for example |
| etc. | (give the full list) |
| i.e. | that is |
| simply, just, easily, please | (remove the word) |
| allows you to | you can |
| in the event that | if |
| a number of | some, many (or give the number) |

## Verbs

1. Use only these tenses: the simple present, the simple past and the simple future
   ("shows", "showed", "will show").
2. Use the active voice in procedures. Use the active voice as much as possible in
   descriptions. Write "The server checks the request", not "The request is checked".
3. Use the imperative in instructions: "Select the model".
4. Do not use "-ing" forms after a verb. Write "Start to type", not "Start typing".

## Sentences

1. Write a maximum of 20 words in a procedural sentence.
2. Write a maximum of 25 words in a descriptive sentence.
3. Write about one topic in each sentence.
4. Do not use semicolons to join sentences. Write two sentences.
5. Use a vertical list when there are more than three items.

## Procedures

1. Write one instruction in each sentence. You can write two instructions in one sentence
   only when you do them at the same time.
2. Write the instructions in the sequence that the user does them.
3. Put a condition before the instruction: "If the model forecasts, type the periods."
4. Use numbered steps for a sequence. Use a bulleted list for items that have no sequence.
5. Use a note to give information that is not an instruction. Do not put an instruction in a
   note.

## Descriptions

1. Start each paragraph with a topic sentence.
2. Write a maximum of six sentences in a paragraph.
3. Give the most important information first.

## Warnings and cautions

1. Start with a clear command: "Do not delete the dataset."
2. Then give the risk: "Model versions that use it will not score."
3. A warning tells about a risk to persons or to security. A caution tells about a risk to
   data or to equipment.

## Text in the app

| Element | Rule | Example |
|---|---|---|
| Button | A verb and, if necessary, an object | "Save spec", "Add a row" |
| Field hint | One or two short sentences. Tell what to type, then the limits. | "Type the number of periods. The maximum is 200." |
| Empty state | Tell what is missing, then what to do | "This project has no datasets. Upload a CSV file to start." |
| Error | Tell what is wrong, then what to do | "The YAML is not correct. Correct the line that shows in red." |
| Tooltip | One sentence | "Shows the inputs that the model needs." |
| Confirmation | Tell the result of the action | "Delete the dataset? You cannot undo this." |

## Project technical names and verbs

You can use these words as technical names (nouns) or technical verbs in this project.

**Technical names:** actual, adstock, API, argument, artifact, backtest, band, binding,
bundle, catalog, channel, coordinate, counterfactual, covariate, CSV file, dashboard, dataset,
dimension (dim), draw, driver, endpoint, evidence pack, forecast, governance, horizon, input
table, intervention, JSON, level, market, metric, mode, model, model version, node, payload,
period, pipeline, plot, plugin, posterior, prior, project, quantile, revision, run, sandbox,
scenario, scorecard, side table, spec, stage, step, summary, target, tenant, token,
variable, webhook, workflow, YAML.

**Technical verbs:** click, deploy, download, fit, forecast, log in, log out, paste, promote,
roll back, score, select, sign in, type, upload.

## Examples

| Not STE | STE |
|---|---|
| Ensure that the dataset has been uploaded prior to scoring. | Upload the dataset before you score the model. |
| The form only offers what this model supports, and the fitted preprocessing is replayed exactly: nothing is refitted. | The form shows only the modes that the model can do. The server uses the same preprocessing as the fit. It does not fit the model again. |
| Clicking Save will create a new revision. | Click **Save revision**. The app makes a new revision. |
| You may want to check the scorecard. | Look at the scorecard. |
