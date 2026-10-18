/** "Your data" for a saved session: what the sandboxed ingest made of the upload. */
import { Link } from "@tanstack/react-router";
import { AlertTriangle, CheckCircle2, RotateCcw } from "lucide-react";

import type { DatasetVersion } from "@/api/types";
import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/primitives";

import type { GuidedSession } from "../use-session";

const ROLE_WORD: Record<string, string> = { target: "the result to explain", spend: "media spend", control: "other drivers", covariate: "drivers", media: "media" };

function coordText(c: NonNullable<NonNullable<DatasetVersion["summary"]>["coords"]>[string]): string {
  if (c.kind === "time") return `${c.size} periods${c.start ? `, ${String(c.start).slice(0, 10)} to ${String(c.end ?? "").slice(0, 10)}` : ""}`;
  const values = (c.values ?? []).map(String);
  return values.length ? `${values.slice(0, 8).join(", ")}${values.length > 8 ? ` and ${values.length - 8} more` : ""}` : `${c.size}`;
}

/** Waiting for, or explaining the failure of, the sandboxed check of the upload. */
export function IngestStatus({ version, compact = false }: { version: DatasetVersion | null; compact?: boolean }) {
  if (!version) return <p className="text-[13px] text-muted-foreground">The app looks for your data…</p>;
  if (version.status === "ingesting") {
    return (
      <p className="flex items-center gap-2 text-[13px]" aria-live="polite">
        <Spinner /> A secure sandbox does a check of your data. This usually takes less than 1 minute.{compact ? "" : " You can continue to work during the check."}
      </p>
    );
  }
  if (version.status === "failed") {
    return (
      <div role="alert" className="grid gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-4 text-[13px]">
        <p className="flex items-center gap-2 font-medium">
          <AlertTriangle aria-hidden className="size-4 text-destructive" /> The app could not read your file
        </p>
        <ul className="grid gap-1 text-muted-foreground">
          {(version.issues ?? []).map((i, n) => (
            <li key={n}>
              {i.message}
              {i.suggested_action ? ` ${i.suggested_action}` : ""}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <p className="flex items-center gap-2 text-[13px]">
      <CheckCircle2 aria-hidden className="size-4 text-success" /> Your data passed the checks.
    </p>
  );
}

export function DataSummaryStep({ session }: { session: GuidedSession }) {
  const v = session.datasetVersion;
  const coords = v?.summary?.coords ?? {};
  const vars = v?.data_schema?.vars ?? {};
  return (
    <div className="grid gap-5">
      <Card>
        <CardContent className="grid gap-4 pt-5">
          <IngestStatus version={v} />
          {v?.status === "ready" ? (
            <dl className="grid gap-x-6 gap-y-3 text-[13px] sm:grid-cols-[minmax(10rem,auto)_1fr]">
              {Object.entries(coords).map(([dim, c]) => (
                <div key={dim} className="contents">
                  <dt className="text-muted-foreground capitalize">{dim === "time" || c.kind === "time" ? "Time" : dim}</dt>
                  <dd className="min-w-0 break-words">{coordText(c)}</dd>
                </div>
              ))}
              {Object.entries(vars).map(([name, spec]) => (
                <div key={name} className="contents">
                  <dt className="font-mono text-[12px] text-muted-foreground">{name}</dt>
                  <dd>{ROLE_WORD[spec.role ?? ""] ?? spec.role ?? "a variable"}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </CardContent>
      </Card>
      <p className="text-[13px] text-muted-foreground">
        To use a different file or to change the meaning of the columns, start a new analysis from the same template. This analysis keeps its results.{" "}
        <Button asChild variant="link" size="sm" className="h-auto px-0">
          <Link to="/guided/new/$templateId" params={{ templateId: session.template.id }}>
            <RotateCcw /> Start again with a new file
          </Link>
        </Button>
      </p>
    </div>
  );
}
