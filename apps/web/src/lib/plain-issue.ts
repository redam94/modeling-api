/**
 * Plain words for a builder field's problem (v0.18 live). The server's checks pass on the
 * validator's own text ("String should match pattern '^[A-Za-z_]…'", "'obs' is not of type
 * 'array'"); a builder field shows what to do instead. Text that matches no known validator
 * shape (the platform's own messages, already plain) comes back unchanged.
 *
 * Pure: no React, no runtime imports.
 */
import type { Issue } from "@/api/types";

/** The patterns of `mapi_spec._types` (and friends), each with what it asks for. */
const PATTERNS: Record<string, string> = {
  "^[A-Za-z_][A-Za-z0-9_.\\-]{0,127}$": "Use letters, digits, _, . and -, and start with a letter or _.",
  "^[a-z_][a-z0-9_]{0,62}$": "Use lower-case letters, digits and _, and start with a letter or _.",
  "^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$": "Use lower-case letters, digits, - and _, and start and end with a letter or digit.",
  "^[a-z][a-z0-9_]{0,63}$": "Use lower-case letters, digits and _, and start with a letter.",
  "^[A-Za-z_][A-Za-z0-9_\\-]{0,63}$": "Use letters, digits, _ and -, and start with a letter or _.",
};

const TYPES: Record<string, string> = {
  string: "Type some text here.",
  number: "Type a number here.",
  integer: "Type a whole number here.",
  boolean: "Choose yes or no here.",
  object: "This needs a group of settings, not a single value.",
  array: "This needs a list. Pick one or more.",
  null: "Leave this empty.",
};

function patternWords(pattern: string): string {
  return PATTERNS[pattern] ?? "Some of these characters are not allowed here.";
}

function choices(raw: string): string {
  const items = [...raw.matchAll(/'([^']*)'/g)].map((m) => m[1]);
  return items.length ? `Choose one of: ${items.join(", ")}.` : "Choose one of the listed values.";
}

type Rule = [RegExp, (m: RegExpMatchArray) => string];

const RULES: Rule[] = [
  // pydantic v2
  [/^Value error, (.+)$/s, (m) => plainIssueMessage(m[1]!)],
  [/^String should match pattern '(.*)'$/s, (m) => patternWords(m[1]!)],
  [/^String should have at most (\d+) characters?$/, (m) => `Keep it to ${m[1]} characters or fewer.`],
  [/^String should have at least (\d+) characters?$/, (m) => (m[1] === "1" ? "Fill this in." : `Use at least ${m[1]} characters.`)],
  [/^Field required$/, () => "Fill this in."],
  [/^Extra inputs are not permitted$/, () => "This setting is not used here. Remove it."],
  [/^Input should be a valid integer/, () => TYPES.integer!],
  [/^Input should be a valid number/, () => TYPES.number!],
  [/^Input should be a valid string/, () => TYPES.string!],
  [/^Input should be a valid boolean/, () => TYPES.boolean!],
  [/^Input should be a valid list/, () => TYPES.array!],
  [/^Input should be a valid dictionary/, () => TYPES.object!],
  [/^Input should be greater than or equal to (\S+)$/, (m) => `Use ${m[1]} or more.`],
  [/^Input should be greater than (\S+)$/, (m) => `Use a number above ${m[1]}.`],
  [/^Input should be less than or equal to (\S+)$/, (m) => `Use ${m[1]} or less.`],
  [/^Input should be less than (\S+)$/, (m) => `Use a number below ${m[1]}.`],
  [/^Input should be (('[^']*'(, | or )?)+)$/, (m) => choices(m[1]!)],
  [/^List should have at most (\d+) items?/, (m) => `Pick at most ${m[1]}.`],
  [/^List should have at least (\d+) items?/, (m) => (m[1] === "1" ? "Pick at least one." : `Pick at least ${m[1]}.`)],
  // jsonschema
  [/^'([^']*)' is a required property$/, (m) => `Fill in ${m[1]}.`],
  [/ is not of type '([a-z]+)'$/s, (m) => TYPES[m[1]!] ?? "This value has the wrong kind here."],
  [/ is not of type (('[a-z]+'(, )?)+)$/s, () => "This value has the wrong kind here."],
  [/ does not match '(.*)'$/s, (m) => patternWords(m[1]!)],
  [/ is not one of \[(.*)\]$/s, (m) => choices(m[1]!)],
  [/ is less than the minimum of (\S+)$/, (m) => `Use ${m[1]} or more.`],
  [/ is greater than the maximum of (\S+)$/, (m) => `Use ${m[1]} or less.`],
  [/ is less than or equal to the minimum of (\S+)$/, (m) => `Use a number above ${m[1]}.`],
  [/ is greater than or equal to the maximum of (\S+)$/, (m) => `Use a number below ${m[1]}.`],
  [/ is too long$/s, () => "This is too long. Shorten it."],
  [/ is too short$/s, () => "This is too short, or empty. Fill it in."],
  [/ should be non-empty$/s, () => "Fill this in."],
  [/^Additional properties are not allowed \('([^']*)' (was|were) unexpected\)$/, (m) => `${m[1]} is not a setting here. Remove it.`],
  [/ has non-unique elements$/s, () => "Each value can be in the list only once."],
];

/** The plain version of one issue message (unchanged when no validator shape matches). */
export function plainIssueMessage(message: string): string {
  const text = message.trim();
  for (const [re, words] of RULES) {
    const m = text.match(re);
    if (m) return words(m);
  }
  return message;
}

/** `issue.message` in plain words. */
export function plainIssue(issue: Pick<Issue, "message">): string {
  return plainIssueMessage(issue.message);
}
