/**
 * Webhooks: the list, creation with an event picker (the signing secret shown once), and a
 * webhook's page - enable/disable, events, secret rotation, a test send, the delivery log with
 * status, attempts and response codes, and replays.
 */
import { Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, Bell, Plus, RefreshCw, RotateCw, Send, Trash2, Webhook as WebhookIcon } from "lucide-react";
import { Fragment, useMemo, useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { ApiError, errorMessage } from "@/api/client";
import {
  useCreateWebhook,
  useDeleteWebhook,
  useDeliveries,
  useReplayDelivery,
  useRotateWebhookSecret,
  useUpdateWebhook,
  useWebhooks,
} from "@/api/integrations";
import type { DeliveryStatus, Webhook, WebhookCreated, WebhookDelivery, WebhookEventType } from "@/api/types";
import { EmptyState, ErrorNotice, Mono, QueryView, Spinner, UntrustedText } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Switch } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { absoluteTime, relativeTime } from "@/lib/format";
import { DELIVERY_META, RETRY_SCHEDULE, validateWebhookForm, WEBHOOK_EVENTS, type WebhookForm } from "@/lib/integrations";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { CodeBlock } from "./endpoint-schema";
import { ShownOnceDialog } from "./shown-once";

const VERIFY_SNIPPET = `from mapi_spec.webhooks import verify_signature

# raw_body: the request body as bytes, exactly as received
if not verify_signature(raw_body, request.headers["Mapi-Signature"], secret):
    return 401  # forged, or older than 5 minutes`;

export function WebhookStatusBadge({ webhook }: { webhook: Webhook }) {
  if (webhook.active) return <Badge variant="success">active</Badge>;
  return (
    <Badge variant={webhook.disabled_reason ? "danger" : "outline"} title={webhook.disabled_reason ?? "Disabled by an admin"}>
      {webhook.disabled_reason ? "disabled automatically" : "disabled"}
    </Badge>
  );
}

/** The webhooks admins see in Integrations (others get an explanation). */
export function WebhooksSection({ onCreate }: { onCreate?: () => void }) {
  const ws = useWorkspace();
  const canRead = can(ws.grants, "read", "webhook");
  const canCreate = can(ws.grants, "create", "webhook");
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<WebhookCreated | null>(null);
  const columns = useMemo<ColumnDef<Webhook, unknown>[]>(
    () => [
      {
        accessorKey: "url",
        header: "Receiver",
        cell: ({ row }) => (
          <div className="grid min-w-0 max-w-96">
            <Link to="/integrations/webhooks/$webhookId" params={{ webhookId: row.original.id }} className="truncate font-mono text-[12px] text-primary hover:underline">
              {row.original.url}
            </Link>
            {row.original.description ? <span className="truncate text-xs text-muted-foreground">{row.original.description}</span> : null}
          </div>
        ),
      },
      {
        id: "events",
        header: "Events",
        accessorFn: (w) => w.events.join(" "),
        cell: ({ row }) => (
          <div className="flex max-w-72 flex-wrap gap-1">
            {row.original.events.map((e) => (
              <Badge key={e} variant="outline" className="font-mono text-[11px]">
                {e}
              </Badge>
            ))}
          </div>
        ),
      },
      { accessorKey: "active", header: "Status", cell: ({ row }) => <WebhookStatusBadge webhook={row.original} /> },
      {
        accessorKey: "last_delivery_at",
        header: "Last delivery",
        cell: ({ row }) => (
          <span className="grid text-[13px]">
            <span title={absoluteTime(row.original.last_delivery_at)}>{row.original.last_delivery_at ? relativeTime(row.original.last_delivery_at) : "Never"}</span>
            {row.original.failure_count ? <span className="text-xs text-warning">{row.original.failure_count} failed in a row</span> : null}
          </span>
        ),
      },
    ],
    [],
  );
  if (!canRead) {
    return (
      <EmptyState icon={<WebhookIcon />} title="Webhooks are managed by project admins">
        A webhook calls a URL when something happens in this project - a run finishes, a model is ready, an endpoint is promoted. Ask a project admin to set one up.
      </EmptyState>
    );
  }
  const openCreate = () => (onCreate ? onCreate() : setCreating(true));
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-[68ch] text-muted-foreground">
          Webhooks call your URL when something happens in this project. Every delivery is signed (<Mono>Mapi-Signature</Mono>), retried with backoff and logged.
        </p>
        <Button onClick={openCreate} disabled={!canCreate}>
          <Plus /> New webhook
        </Button>
      </div>
      <WebhooksTable columns={columns} onRowClick={(w) => void navigate({ to: "/integrations/webhooks/$webhookId", params: { webhookId: w.id } })} onCreate={openCreate} canCreate={canCreate} />
      {creating ? (
        <CreateWebhookDialog
          onClose={() => setCreating(false)}
          onCreated={(c) => {
            setCreating(false);
            setCreated(c);
          }}
        />
      ) : null}
      {created ? <WebhookSecretDialog created={created} onClose={() => setCreated(null)} /> : null}
    </div>
  );
}

function WebhooksTable({ columns, onRowClick, onCreate, canCreate }: { columns: ColumnDef<Webhook, unknown>[]; onRowClick: (w: Webhook) => void; onCreate: () => void; canCreate: boolean }) {
  const webhooks = useWebhooks();
  return (
    <QueryView query={webhooks}>
      {(page) =>
        page.items.length ? (
          <DataTable label="Webhooks" data={page.items} columns={columns} getRowId={(w) => w.id} onRowClick={onRowClick} />
        ) : (
          <EmptyState
            icon={<Bell />}
            title="No webhooks yet"
            action={
              canCreate ? (
                <Button onClick={onCreate}>
                  <Plus /> Create a webhook
                </Button>
              ) : null
            }
          >
            Hear about finished runs, ready models and promotions in your own systems, without polling.
          </EmptyState>
        )
      }
    </QueryView>
  );
}

// ----------------------------------------------------------------------- create

export function EventPicker({ value, onChange, idPrefix = "evt" }: { value: WebhookEventType[]; onChange: (v: WebhookEventType[]) => void; idPrefix?: string }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {WEBHOOK_EVENTS.map((e) => {
        const checked = value.includes(e.type);
        const id = `${idPrefix}-${e.type.replace(/\./g, "-")}`;
        return (
          <label key={e.type} htmlFor={id} className={cn("flex cursor-pointer items-start gap-3 rounded-lg border p-3 hover:bg-accent/40", checked && "border-primary/50 bg-accent/40")}>
            <Checkbox id={id} checked={checked} onCheckedChange={(v) => onChange(v === true ? [...value, e.type] : value.filter((x) => x !== e.type))} className="mt-0.5" />
            <span className="grid gap-0.5">
              <span className="flex flex-wrap items-center gap-2 text-[13px] font-medium">
                {e.label} <Mono className="text-muted-foreground">{e.type}</Mono>
              </span>
              <span className="text-xs text-muted-foreground">{e.description}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

export function CreateWebhookDialog({ initialEvents, onClose, onCreated }: { initialEvents?: WebhookEventType[]; onClose: () => void; onCreated: (c: WebhookCreated) => void }) {
  const create = useCreateWebhook();
  const [form, setForm] = useState<WebhookForm>({ url: "", description: "", events: initialEvents ?? ["run.succeeded", "run.failed", "model.version.ready"] });
  const [submitted, setSubmitted] = useState(false);
  const errors = validateWebhookForm(form);
  const blocked = create.error instanceof ApiError && create.error.code === "WEBHOOK_URL_BLOCKED" ? (create.error.problem?.errors?.[0]?.message ?? create.error.problem?.detail ?? "This URL is not allowed.") : null;
  const shown = submitted ? errors : {};
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (Object.keys(errors).length) return;
    create.mutate({ url: form.url.trim(), events: form.events, ...(form.description.trim() ? { description: form.description.trim() } : {}) }, { onSuccess: onCreated });
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>New webhook</DialogTitle>
        <DialogDescription>The platform POSTs a signed JSON envelope to your URL for each event you pick. Envelopes carry ids and states, never data values or secrets.</DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <Field label="URL" htmlFor="wh-url" error={shown.url ?? blocked} hint="https on port 443 or 8443, on a public host. Internal and private addresses are refused.">
            <Input id="wh-url" type="url" autoFocus className="font-mono" placeholder="https://hooks.example.com/mapi" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </Field>
          <Field label="Description (optional)" htmlFor="wh-desc" error={shown.description}>
            <Input id="wh-desc" maxLength={500} placeholder="Planning pipeline" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </Field>
          <fieldset className="grid gap-2" aria-describedby={shown.events ? "wh-events-error" : undefined}>
            <legend className="mb-1 text-[13px] font-medium">Events</legend>
            <EventPicker value={form.events} onChange={(events) => setForm({ ...form, events })} />
            {shown.events ? (
              <p id="wh-events-error" role="alert" className="text-xs font-medium text-destructive">
                {shown.events}
              </p>
            ) : null}
          </fieldset>
          {create.isError && !blocked ? <ErrorNotice error={create.error} title="The webhook was not created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : <WebhookIcon />} Create webhook
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function WebhookSecretDialog({ created, rotated = false, onClose }: { created: WebhookCreated; rotated?: boolean; onClose: () => void }) {
  return (
    <ShownOnceDialog
      title={rotated ? "Copy the new signing secret" : "Copy the signing secret now"}
      description={
        <>
          This is the only time the secret for <Mono>{created.webhook.url}</Mono> is shown. Your receiver verifies every delivery with it.
          {rotated ? " The old secret keeps signing for 24 hours, so you can switch over without dropping events." : ""}
        </>
      }
      label="Signing secret"
      value={created.secret}
      copyLabel="Copy signing secret"
      onClose={onClose}
    >
      <details className="rounded-md border p-3 text-[13px]">
        <summary className="cursor-pointer font-medium">Verify a delivery (Python)</summary>
        <div className="mt-2">
          <CodeBlock code={VERIFY_SNIPPET} label="Signature check" language="python" />
          <p className="mt-2 text-xs text-muted-foreground">
            <Mono>v1</Mono> is the hex HMAC-SHA256 of <Mono>&lt;t&gt;.&lt;raw body&gt;</Mono>; compare in constant time and refuse timestamps more than 300 s off.
          </p>
        </div>
      </details>
    </ShownOnceDialog>
  );
}

// ----------------------------------------------------------------------- one webhook

export function AutoDisabledNotice({ webhook }: { webhook: Webhook }) {
  if (webhook.active || !webhook.disabled_reason) return null;
  return (
    <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-4">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="grid gap-1">
        <p className="font-semibold">This webhook was disabled automatically</p>
        <p className="text-muted-foreground">{webhook.disabled_reason}</p>
        <p className="text-[13px]">Fix the receiver, then enable the webhook again: that resets the failure count. Events while it was disabled were not queued.</p>
      </div>
    </div>
  );
}

const DELIVERY_FILTERS: (DeliveryStatus | "")[] = ["", "succeeded", "retrying", "failed", "blocked", "pending"];

export function WebhookSettings({ webhook }: { webhook: Webhook }) {
  const ws = useWorkspace();
  const canUpdate = can(ws.grants, "update", "webhook");
  const update = useUpdateWebhook(webhook.id);
  const rotate = useRotateWebhookSecret(webhook.id);
  const [events, setEvents] = useState<WebhookEventType[]>(webhook.events);
  const [rotating, setRotating] = useState(false);
  const [rotated, setRotated] = useState<WebhookCreated | null>(null);
  const dirty = events.join() !== webhook.events.join();
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Settings</CardTitle>
          <CardDescription>
            Signing secret <Mono>{webhook.secret_hint}</Mono> · created {relativeTime(webhook.created_at)}
          </CardDescription>
        </div>
        <label className="flex items-center gap-2 text-[13px]">
          <Switch
            checked={webhook.active}
            disabled={!canUpdate || update.isPending}
            aria-label="Webhook enabled"
            onCheckedChange={(active) =>
              update.mutate(
                { active },
                {
                  onSuccess: () => toast.success(active ? "Webhook enabled" : "Webhook disabled", active ? "The failure count was reset." : undefined),
                  onError: (e) => toast.error("Change failed", errorMessage(e)),
                },
              )
            }
          />
          {webhook.active ? "Enabled" : "Disabled"}
        </label>
      </CardHeader>
      <CardContent className="grid gap-4">
        <fieldset className="grid gap-2" disabled={!canUpdate}>
          <legend className="mb-1 text-[13px] font-medium">Events</legend>
          <EventPicker value={events} onChange={setEvents} idPrefix="wh-edit" />
        </fieldset>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={!dirty || !events.length || update.isPending}
            onClick={() => update.mutate({ events }, { onSuccess: () => toast.success("Events saved"), onError: (e) => toast.error("Save failed", errorMessage(e)) })}
          >
            Save events
          </Button>
          <Button size="sm" variant="outline" disabled={!canUpdate} onClick={() => setRotating(true)}>
            <RotateCw /> Rotate secret
          </Button>
        </div>
      </CardContent>
      {rotating ? (
        <Dialog open onOpenChange={(o) => !o && setRotating(false)}>
          <DialogContent>
            <DialogTitle>Rotate the signing secret?</DialogTitle>
            <DialogDescription>A new secret is shown once. Deliveries carry a signature from both secrets for 24 hours, then only from the new one.</DialogDescription>
            {rotate.isError ? <ErrorNotice error={rotate.error} /> : null}
            <DialogFooter>
              <Button variant="ghost" onClick={() => setRotating(false)}>
                Cancel
              </Button>
              <Button
                disabled={rotate.isPending}
                onClick={() =>
                  rotate.mutate(undefined, {
                    onSuccess: (c) => {
                      setRotating(false);
                      setRotated(c);
                    },
                  })
                }
              >
                <RotateCw /> Rotate
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
      {rotated ? <WebhookSecretDialog created={rotated} rotated onClose={() => setRotated(null)} /> : null}
    </Card>
  );
}

function DeliveryRow({ d, onReplay, canReplay, replaying, open, onToggle }: { d: WebhookDelivery; onReplay: () => void; canReplay: boolean; replaying: boolean; open: boolean; onToggle: () => void }) {
  const meta = DELIVERY_META[d.status];
  const hasDetails = Boolean(d.error || d.response_excerpt || d.next_retry_at || d.replay_of);
  return (
    <>
      <TR data-status={d.status}>
        <TD>
          <Mono>{d.event_type}</Mono>
          <span className="block text-xs text-muted-foreground">
            <Mono>{d.event_id}</Mono>
          </span>
        </TD>
        <TD>
          <Badge variant={meta.variant} title={meta.description}>
            {meta.label}
          </Badge>
          {d.replay_of ? <span className="block text-xs text-muted-foreground">replay</span> : null}
        </TD>
        <TD className="tabular">{d.attempt}</TD>
        <TD className="tabular">{d.response_code ?? "—"}</TD>
        <TD className="tabular text-muted-foreground">{d.duration_ms != null ? `${d.duration_ms} ms` : "—"}</TD>
        <TD>
          <span title={absoluteTime(d.created_at)}>{relativeTime(d.created_at)}</span>
        </TD>
        <TD className="text-right whitespace-nowrap">
          {hasDetails ? (
            <Button variant="ghost" size="sm" aria-expanded={open} onClick={onToggle}>
              Details
            </Button>
          ) : null}
          <Button variant="ghost" size="sm" disabled={!canReplay || replaying} onClick={onReplay} aria-label={`Replay delivery ${d.id}`}>
            {replaying ? <Spinner /> : <RefreshCw />} Replay
          </Button>
        </TD>
      </TR>
      {open ? (
        <TR>
          <TD colSpan={7} className="bg-muted/30">
            <div className="grid gap-2 py-1 text-[13px]">
              {d.next_retry_at ? <p>Next try {relativeTime(d.next_retry_at)} ({absoluteTime(d.next_retry_at)}).</p> : null}
              {d.replay_of ? (
                <p>
                  Replay of <Mono>{d.replay_of}</Mono>.
                </p>
              ) : null}
              {d.error ? <p className="text-destructive">{d.error}</p> : null}
              {d.response_excerpt ? (
                <div>
                  <p className="text-xs text-muted-foreground">Your receiver answered (untrusted, first 1 KiB):</p>
                  <UntrustedText text={d.response_excerpt} className="mt-1" maxHeight="10rem" />
                </div>
              ) : null}
            </div>
          </TD>
        </TR>
      ) : null}
    </>
  );
}

export function DeliveryLog({ webhook }: { webhook: Webhook }) {
  const ws = useWorkspace();
  const canReplay = can(ws.grants, "update", "webhook") && webhook.active;
  const [status, setStatus] = useState<DeliveryStatus | "">("");
  const [open, setOpen] = useState<string | null>(null);
  const deliveries = useDeliveries(webhook.id, status || null);
  const replay = useReplayDelivery(webhook.id);
  const [pending, setPending] = useState<string | null>(null);
  const doReplay = (d: WebhookDelivery, label: string) => {
    setPending(d.id);
    replay.mutate(d.id, {
      onSuccess: () => toast.success(label, `${d.event_type} is queued for ${webhook.url}`),
      onError: (e) => toast.error(`${label} failed`, errorMessage(e)),
      onSettled: () => setPending(null),
    });
  };
  const newest = deliveries.data?.items[0];
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Delivery log</CardTitle>
          <CardDescription>Every try, newest first. Failed deliveries are retried after {RETRY_SCHEDULE.join(", ")}.</CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <NativeSelect aria-label="Filter deliveries by status" className="h-8 w-40" value={status} onChange={(e) => setStatus(e.target.value as DeliveryStatus | "")}>
            {DELIVERY_FILTERS.map((s) => (
              <option key={s} value={s}>
                {s ? DELIVERY_META[s].label : "All deliveries"}
              </option>
            ))}
          </NativeSelect>
          <Button
            size="sm"
            variant="outline"
            disabled={!canReplay || !newest || pending !== null}
            title={!webhook.active ? "Enable the webhook first" : !newest ? "Nothing was delivered yet: the first matching event will be" : "Send the newest event again"}
            onClick={() => newest && doReplay(newest, "Test delivery sent")}
          >
            <Send /> Send test
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <QueryView query={deliveries}>
          {(page) =>
            page.items.length ? (
              <Table aria-label="Deliveries">
                <THead>
                  <TR>
                    <TH>Event</TH>
                    <TH>Status</TH>
                    <TH>Attempts</TH>
                    <TH>Response</TH>
                    <TH>Took</TH>
                    <TH>When</TH>
                    <TH>
                      <span className="sr-only">Actions</span>
                    </TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((d) => (
                    <Fragment key={d.id}>
                      <DeliveryRow d={d} canReplay={canReplay} replaying={pending === d.id} onReplay={() => doReplay(d, "Replay queued")} open={open === d.id} onToggle={() => setOpen(open === d.id ? null : d.id)} />
                    </Fragment>
                  ))}
                </TBody>
              </Table>
            ) : (
              <EmptyState icon={<Send />} title={status ? "No deliveries with this status" : "Nothing delivered yet"}>
                Deliveries appear here as soon as a subscribed event happens in the project.
              </EmptyState>
            )
          }
        </QueryView>
      </CardContent>
    </Card>
  );
}

export function DeleteWebhookDialog({ webhook, onClose, onDeleted }: { webhook: Webhook; onClose: () => void; onDeleted: () => void }) {
  const del = useDeleteWebhook();
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Delete this webhook?</DialogTitle>
        <DialogDescription>
          <Mono>{webhook.url}</Mono> stops receiving events at once, and its delivery log is deleted.
        </DialogDescription>
        {del.isError ? <ErrorNotice error={del.error} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            disabled={del.isPending}
            onClick={() =>
              del.mutate(webhook.id, {
                onSuccess: () => {
                  toast.success("Webhook deleted");
                  onDeleted();
                },
              })
            }
          >
            <Trash2 /> Delete webhook
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

