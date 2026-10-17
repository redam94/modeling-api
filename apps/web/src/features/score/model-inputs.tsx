/**
 * "What this model needs": the version's input contract (`GET …/inputs`) in plain words - what
 * it predicts, which inputs it reads (and which it needs on future periods), and what it was
 * fitted on - so a user knows what to type before scoring.
 */
import { CircleHelp, Target } from "lucide-react";

import type { ModelInputSchema } from "@/api/types";
import { Mono } from "@/components/common";
import { Badge } from "@/components/ui/primitives";

import { fittedRange, freqUnit, inputVars } from "./inputs";

const ROLE_LABEL: Record<string, string> = {
  target: "predicts",
  spend: "spend",
  covariate: "driver",
  control: "control",
  exposure: "exposure",
  weight: "weight",
  offset: "offset",
};

export function ModelInputsPanel({ inputs }: { inputs: ModelInputSchema }) {
  const snap = inputs.snapshot;
  const vars = inputVars(snap);
  const dims = Object.entries(snap.input_schema.dims ?? {});
  const targets = vars.filter((v) => v.role === "target");
  const others = vars.filter((v) => v.role !== "target");
  const hdim = snap.horizon_dim;
  const unit = hdim ? freqUnit(snap.input_schema.dims?.[hdim]?.freq ?? snap.fit_coords?.[hdim]?.freq) : null;

  return (
    <section aria-label="What this model needs" className="grid gap-3 rounded-lg border bg-muted/30 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Target className="size-4 text-primary" aria-hidden />
        <h3 className="text-[13px] font-semibold">What this model needs</h3>
      </div>
      <p className="text-[13px]">
        It predicts{" "}
        {targets.length ? (
          targets.map((t, i) => (
            <span key={t.name}>
              {i ? ", " : ""}
              <Mono>{t.name}</Mono>
              {t.units ? ` (${t.units})` : ""}
            </span>
          ))
        ) : (
          "its outputs"
        )}
        {dims.length ? (
          <>
            {" "}
            per{" "}
            {dims.map(([d], i) => (
              <span key={d}>
                {i ? (i === dims.length - 1 ? " and " : ", ") : ""}
                <Mono>{d}</Mono>
              </span>
            ))}
          </>
        ) : null}
        .
        {hdim ? (
          <>
            {" "}
            It can forecast past the end of its fitted <Mono>{hdim}</Mono>
            {unit ? `, one ${unit} at a time` : ""}.
          </>
        ) : null}
      </p>

      {others.length ? (
        <div className="grid gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">Inputs it reads</span>
          <ul className="grid gap-1">
            {others.map((v) => (
              <li key={v.name} className="flex flex-wrap items-center gap-2 text-[13px]">
                <Mono>{v.name}</Mono>
                {(ROLE_LABEL[v.role] ?? v.role) !== v.name ? <Badge variant="outline">{ROLE_LABEL[v.role] ?? v.role}</Badge> : null}
                {v.future ? (
                  <Badge variant="warning" title="Forecasts need a value on every period they cover: type it, or choose how to fill it in">
                    needed on future periods
                  </Badge>
                ) : null}
                <span className="text-xs text-muted-foreground">
                  by {v.dims.join(" × ") || "nothing"}
                  {v.units ? ` · ${v.units}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">It reads no inputs besides its own history: a forecast needs nothing typed in.</p>
      )}

      {dims.length ? (
        <div className="grid gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">What it was fitted on</span>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[13px]">
            {dims.map(([d, spec]) => (
              <div key={d} className="contents">
                <dt>
                  <Mono>{d}</Mono>
                </dt>
                <dd className="min-w-0 text-muted-foreground">
                  {fittedRange(snap.fit_coords?.[d]) ?? spec.kind}
                  {spec.policy === "new_levels" ? " · new levels allowed" : ""}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
      <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
        <CircleHelp className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        Names are the spec&apos;s canonical names (its <Mono>data.bind</Mono>), which is what typed values use.
      </p>
    </section>
  );
}
