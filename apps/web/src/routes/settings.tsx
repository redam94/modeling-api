import { LogOut } from "lucide-react";

import { ROLE_META } from "@/api/access";
import { useReady } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { useSignOut } from "@/features/auth/gate";
import { useWorkspace } from "@/features/auth/workspace";
import { useEffectivePersona, useGuidedLock, useSetPersona } from "@/features/guided/persona";
import { KeyValues, Mono, PageHeader } from "@/components/common";
import { SegmentedControl } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { env } from "@/env";
import { absoluteTime, formatNumber } from "@/lib/format";
import { useUi, type Persona, type ThemePreference } from "@/stores/ui";

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
      <PageHeader title="Settings" description="Preferences are stored in this browser." />
      <div className="grid max-w-3xl gap-5">
        {ws.mode === "session" ? (
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Account</CardTitle>
                <CardDescription>Signed in through your organisation; the session cookie is HTTP-only.</CardDescription>
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
              <CardDescription>System follows your operating system setting.</CardDescription>
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
                Expert mode edits specs directly. Guided mode walks analysts through tasks in business terms; both edit the same spec. The choice is remembered for your account on this browser.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            {lock.locked ? (
              <p className="text-[13px] text-muted-foreground">A project admin has locked this project to Guided mode.</p>
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
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Connection</CardTitle>
              <CardDescription>Set at build time with VITE_API_BASE and VITE_PLOTS_ORIGIN.</CardDescription>
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
      </div>
    </>
  );
}
