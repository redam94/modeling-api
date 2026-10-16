/** Step 1, "What do you want to learn?": the templates as friendly cards, plus the analyses in progress. */
import { Link } from "@tanstack/react-router";
import { ArrowRight, BarChart3, Clock, LineChart, Megaphone, ShoppingBasket, Store } from "lucide-react";

import { useSpecs } from "@/api/queries";
import { EmptyState, ErrorNotice, LoadingRows } from "@/components/common";
import { Badge } from "@/components/ui/primitives";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { guidedSessions, useGuidedTemplates } from "../api";
import { guidedTemplateOf, hasTask } from "../session";
import type { GuidedTemplate } from "../types";

const ICONS = { mmm: Megaphone, forecast: LineChart, tabular: Store, choice: ShoppingBasket } as const;

const FOR: Record<string, string[]> = {
  geo_mmm: ["Weekly spend per channel and results per market", "ROI per channel, what-if budgets"],
  weekly_forecast: ["One weekly or daily series", "A forecast with a likely range"],
  store_demand: ["Units per store and week, with prices or promotions", "Low, likely and high demand"],
  customer_choice: ["Shopping trips per household with prices per product", "Which product wins, and why"],
};

/** The plan's order (§11): the marketing mix first, then forecasts, demand and choice. */
const ORDER = ["geo_mmm", "weekly_forecast", "store_demand", "customer_choice"];

export function sortTemplates(items: readonly GuidedTemplate[]): GuidedTemplate[] {
  const rank = (id: string) => (ORDER.includes(id) ? ORDER.indexOf(id) : ORDER.length);
  return [...items].sort((a, b) => rank(a.id) - rank(b.id) || a.title.localeCompare(b.title));
}

function taskBadges(t: GuidedTemplate): string[] {
  const out: string[] = [];
  if (hasTask(t, "whatif")) out.push("What-if budgets");
  if (hasTask(t, "forecast")) out.push("Forecasts");
  if (hasTask(t, "counterfactual") && !hasTask(t, "whatif")) out.push("Scenarios");
  if (hasTask(t, "business_priors")) out.push("Your beliefs");
  if (hasTask(t, "lift_tests")) out.push("Experiments");
  return out;
}

export function TemplateCard({ template }: { template: GuidedTemplate }) {
  const Icon = ICONS[template.family] ?? BarChart3;
  const available = template.plugin_available !== false;
  const body = (
    <>
      <div className="flex items-start justify-between gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border bg-accent text-primary">
          <Icon className="size-5" aria-hidden />
        </span>
        {available ? <ArrowRight aria-hidden className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" /> : <Badge variant="outline">Coming soon</Badge>}
      </div>
      <div className="grid gap-1.5">
        <h2 className="text-[15px] font-semibold tracking-tight">{template.title}</h2>
        <p className="text-[13px] text-muted-foreground">{template.description_plain}</p>
      </div>
      {FOR[template.id] ? (
        <ul className="grid gap-1 text-xs text-muted-foreground">
          {FOR[template.id]?.map((line) => (
            <li key={line} className="flex gap-2">
              <span aria-hidden className="mt-1.5 size-1 shrink-0 rounded-full bg-muted-foreground/60" />
              {line}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
        {taskBadges(template).map((b) => (
          <Badge key={b} variant="secondary">
            {b}
          </Badge>
        ))}
      </div>
      {!available && template.plugin_note ? <p className="text-xs text-muted-foreground">{template.plugin_note}</p> : null}
    </>
  );
  const cls = "group flex h-full flex-col gap-4 rounded-xl border bg-card p-5 text-left shadow-[0_1px_0_0_rgb(0_0_0/0.03)] transition-colors";
  return available ? (
    <Link
      to="/guided/new/$templateId"
      params={{ templateId: template.id }}
      className={cn(cls, "hover:border-primary/40 hover:bg-accent/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring")}
      aria-label={`${template.title}: ${template.description_plain}`}
    >
      {body}
    </Link>
  ) : (
    <div className={cn(cls, "opacity-70")} aria-disabled>
      {body}
    </div>
  );
}

export function TemplatePicker() {
  const templates = useGuidedTemplates();
  if (templates.isPending) return <LoadingRows rows={3} />;
  if (templates.isError) return <ErrorNotice error={templates.error} title="The templates could not be loaded" onRetry={() => void templates.refetch()} />;
  return (
    <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Templates">
      {sortTemplates(templates.data.items).map((t) => (
        <li key={t.id}>
          <TemplateCard template={t} />
        </li>
      ))}
    </ul>
  );
}

/** Saved Guided analyses, most recent first: each opens where it stands. */
export function SessionList() {
  const specs = useSpecs();
  const templates = useGuidedTemplates();
  const sessions = guidedSessions(specs.data?.items);
  if (specs.isPending) return null;
  if (!sessions.length) {
    return (
      <EmptyState icon={<Clock />} title="Nothing in progress yet" className="p-6">
        Your analyses appear here, so you can pick up where you left off.
      </EmptyState>
    );
  }
  const title = (id: string | null) => templates.data?.items.find((t) => t.id === id)?.title ?? id ?? "";
  return (
    <ul className="grid gap-2" aria-label="Your analyses">
      {sessions.slice(0, 8).map((s) => {
        const tid = guidedTemplateOf(s.latest?.document);
        return (
          <li key={s.id}>
            <Link
              to="/guided/$specId"
              params={{ specId: s.id }}
              className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-lg border bg-card px-4 py-3 hover:bg-accent/50 focus-visible:outline-2 focus-visible:outline-ring"
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{s.name}</span>
                <span className="text-xs text-muted-foreground">{title(tid)}</span>
              </span>
              <span className="text-xs text-muted-foreground">updated {relativeTime(s.updated_at)}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
