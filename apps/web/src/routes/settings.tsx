import { LogOut, RotateCcw } from "lucide-react";

import { can, ROLE_META } from "@/api/access";
import { errorMessage } from "@/api/client";
import { useReady, useUpdateProject } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { useSignOut } from "@/features/auth/gate";
import { useWorkspace } from "@/features/auth/workspace";
import { useEffectivePersona, useGuidedLock, useSetPersona } from "@/features/guided/persona";
import { useOnboarding, useOnboardingTenant, usePatchOnboarding, useResetOnboarding } from "@/features/onboarding/api";
import { DataSettings } from "@/features/trust/data-settings";
import { KeyValues, Mono, PageHeader } from "@/components/common";
import { SegmentedControl, Switch } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { env } from "@/env";
import { absoluteTime, formatNumber } from "@/lib/format";
import { toast } from "@/stores/toast";
import { useUi, type Persona, type ThemePreference } from "@/stores/ui";

/** The project's persona lock (`Project.guided_locked`), for its admins. */
function GuidedLockSwitch({ locked }: { locked: boolean }) {
  const ws = useWorkspace();
  const current = ws.current;
  const update = useUpdateProject(current?.tenant.id ?? "", current?.project.project_id ?? "");
  if (ws.mode !== "session" || !current || !can(ws.grants, "update", "project")) return null;
  return (
    <div className="mt-4 flex items-start gap-3 border-t pt-4">
      <Switch
        id="guided-lock"
        className="mt-0.5"
        checked={locked}
        disabled={update.isPending}
        aria-describedby="guided-lock-hint"
        onCheckedChange={(on) =>
          update.mutate(
            { guided_locked: on },
            {
              onSuccess: () => toast.success(on ? "Locked to Guided mode" : "Guided lock removed", current.project.name),
              onError: (err) => toast.error("The app could not change the lock", errorMessage(err)),
            },
          )
        }
      />
      <div className="grid gap-0.5">
        <label htmlFor="guided-lock" className="text-sm font-medium">
          Lock {current.project.name} to Guided mode
        </label>
        <p id="guided-lock-hint" className="text-xs text-muted-foreground">
          Members below admin see only Guided screens. They can answer the questions, give their beliefs, run analyses and share them. They cannot change to
          Expert mode or change advanced settings. Project admins keep both modes. The audit log records each change.
        </p>
      </div>
    </div>
  );
}

/**
 * v0.18 (D19, D24): tips, tours and the first-run checklist, per user in this organisation.
 * "Reset tips" starts them all again (the welcome card, the checklist and the tours).
 */
function TipsCard() {
  const tenant = useOnboardingTenant();
  const progress = useOnboarding();
  const patch = usePatchOnboarding();
  const reset = useResetOnboarding();
  if (!tenant) return null;
  const tipsOn = progress.data ? !progress.data.tips_off : true;
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Tips and tours</CardTitle>
          <CardDescription>Your checklist, tours and tips follow you to any browser. They are yours alone, so nobody else in your team sees them change.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex items-start gap-3">
          <Switch
            id="tips-on"
            className="mt-0.5"
            checked={tipsOn}
            disabled={!progress.data || patch.isPending}
            aria-describedby="tips-on-hint"
            onCheckedChange={(on) =>
              patch.mutate(
                { patch: { tips_off: !on } },
                { onError: (err) => toast.error("The app could not save the change", errorMessage(err)) },
              )
            }
          />
          <div className="grid gap-0.5">
            <label htmlFor="tips-on" className="text-sm font-medium">
              Show tips and the Start here checklist
            </label>
            <p id="tips-on-hint" className="text-xs text-muted-foreground">
              Turn this off and Home folds Start here into one line. You can still take any tour from your account menu.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3 border-t pt-4">
          <Button
            variant="outline"
            size="sm"
            disabled={reset.isPending}
            onClick={() =>
              reset.mutate(undefined, {
                onSuccess: () => toast.success("Tips are back", "Your checklist and tours start fresh. The welcome card shows on your next visit."),
                onError: (err) => toast.error("The app could not reset the tips", errorMessage(err)),
              })
            }
          >
            <RotateCcw /> Reset tips
          </Button>
          <p className="text-xs text-muted-foreground">Starts the checklist, the tours and the welcome card again.</p>
        </div>
      </CardContent>
    </Card>
  );
}

export function SettingsPage() {
  const theme = useUi((s) => s.theme);
  const setTheme = useUi((s) => s.setTheme);
  const persona = useEffectivePersona();
  const setPersona = useSetPersona();
  const lock = useGuidedLock();
  const ready = useReady();
  const ws = useWorkspace();
  const signOut = useSignOut();
  return (
    <>
      <PageHeader title="Settings" description="This browser keeps your preferences." />
      <div className="grid max-w-3xl gap-5">
        {ws.mode === "session" ? (
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Account</CardTitle>
                <CardDescription>You signed in through your organisation. The session cookie is HTTP-only.</CardDescription>
              </div>
              <Button variant="outline" size="sm" onClick={() => void signOut()}>
                <LogOut /> Sign out
              </Button>
            </CardHeader>
            <CardContent>
              <KeyValues
                items={[
                  ["Name", ws.user.name],
                  ["E-mail", ws.user.email ?? "—"],
                  ["User id", <Mono key="id">{ws.user.id}</Mono>],
                  ["Platform operator", ws.operator ? "Yes" : "No"],
                  ["Session ends", ws.expiresAt ? absoluteTime(ws.expiresAt) : "—"],
                  [
                    "Current project",
                    ws.current ? (
                      <span key="p">
                        {ws.current.tenant.name} / {ws.current.project.name} <span className="text-muted-foreground">· {ROLE_META[ws.current.role].label}</span>
                      </span>
                    ) : (
                      "—"
                    ),
                  ],
                ]}
              />
            </CardContent>
          </Card>
        ) : null}
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Appearance</CardTitle>
              <CardDescription>The System theme uses the setting of your operating system.</CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            <SegmentedControl<ThemePreference>
              label="Theme"
              value={theme}
              onValueChange={setTheme}
              options={[
                { value: "system", label: "System" },
                { value: "light", label: "Light" },
                { value: "dark", label: "Dark" },
              ]}
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Mode</CardTitle>
              <CardDescription>
                In Expert mode, you edit specs directly. In Guided mode, the app takes analysts through tasks in business terms. The two modes edit the same spec. This browser keeps your choice for your account.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            {lock.locked ? (
              <p className="text-sm text-muted-foreground">A project admin locked this project to Guided mode.</p>
            ) : (
              <SegmentedControl<Persona>
                label="Mode"
                value={persona}
                onValueChange={setPersona}
                options={[
                  { value: "expert", label: "Expert" },
                  { value: "guided", label: "Guided" },
                ]}
              />
            )}
            <GuidedLockSwitch locked={lock.projectLocked} />
          </CardContent>
        </Card>
        {ws.mode === "session" ? <TipsCard /> : null}
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Connection</CardTitle>
              <CardDescription>You set these values at build time with VITE_API_BASE and VITE_PLOTS_ORIGIN.</CardDescription>
            </div>
            {ready.data ? (
              <Badge variant={ready.data.status === "ok" ? "success" : ready.data.status === "degraded" ? "warning" : "danger"}>{ready.data.status}</Badge>
            ) : ready.isError ? (
              <Badge variant="danger">unreachable</Badge>
            ) : null}
          </CardHeader>
          <CardContent>
            <KeyValues
              items={[
                ["API", <Mono key="a">{env.apiBase}</Mono>],
                ["Plot sandbox", <Mono key="p">{env.plotsOrigin}</Mono>],
                ["Server version", ready.data?.version ?? "—"],
                ["Contract", ready.data ? `${ready.data.contract_version} (${ready.data.api_version ?? "v1"})` : "—"],
                ...Object.entries(ready.data?.checks ?? {}).map(([name, c]): [string, React.ReactNode] => [
                  name,
                  <span key={name}>
                    {c.status}
                    {c.latency_ms != null ? <span className="text-muted-foreground"> · {formatNumber(c.latency_ms, 3)} ms</span> : null}
                    {c.detail ? <span className="block text-xs text-muted-foreground">{c.detail}</span> : null}
                  </span>,
                ]),
              ]}
            />
          </CardContent>
        </Card>
        {ws.mode === "session" && ws.current?.tenantRole === "admin" && "trust_tier" in ws.current.tenant ? <DataSettings tenant={ws.current.tenant} /> : null}
      </div>
    </>
  );
}
