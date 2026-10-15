import { useReady } from "@/api/queries";
import { KeyValues, Mono, PageHeader } from "@/components/common";
import { SegmentedControl } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { env } from "@/env";
import { formatNumber } from "@/lib/format";
import { useUi, type Persona, type ThemePreference } from "@/stores/ui";

export function SettingsPage() {
  const theme = useUi((s) => s.theme);
  const setTheme = useUi((s) => s.setTheme);
  const persona = useUi((s) => s.persona);
  const setPersona = useUi((s) => s.setPersona);
  const ready = useReady();
  return (
    <>
      <PageHeader title="Settings" description="Preferences are stored in this browser." />
      <div className="grid max-w-3xl gap-5">
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
                Expert mode edits specs directly. Guided mode walks analysts through tasks in business terms; it arrives in v0.4, and both modes edit the same spec.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            <SegmentedControl<Persona>
              label="Mode"
              value={persona}
              onValueChange={setPersona}
              options={[
                { value: "expert", label: "Expert" },
                { value: "guided", label: "Guided" },
              ]}
            />
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
