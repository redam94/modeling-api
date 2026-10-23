/**
 * Settings › Workspace › Uploaded code (D20; tenant admins only): whether uploaders may use
 * their own tested uploads before a review (private use, on by default), the workspace's
 * tenant code (in every uploaded id), why uploads are or are not allowed here, and who may
 * upload (verified uploaders).
 */
import { errorMessage } from "@/api/client";
import { ErrorNotice, KeyValues, LoadingRows, Mono } from "@/components/common";
import { Switch } from "@/components/ui/overlays";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

import { pluginFlags, usePluginSettings, useUpdatePluginSettings } from "./api";
import { DevBanner } from "./dev-banner";
import { UploadersList } from "./uploaders-list";

export default function PluginSettingsCard({ tenantId }: { tenantId: string }) {
  const ws = useWorkspace();
  const settings = usePluginSettings(tenantId);
  const update = useUpdatePluginSettings(tenantId);
  if (!pluginFlags(ws.me).enabled) return null;
  const s = settings.data;
  return (
    <Card data-testid="plugin-settings-card">
      <CardHeader>
        <div>
          <CardTitle>Uploaded code</CardTitle>
          <CardDescription>Python packages your team uploads. Each version is checked and tested in a sandbox, and a second admin approves it for everyone.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-5">
        <DevBanner />
        {settings.isPending ? (
          <LoadingRows rows={3} />
        ) : settings.isError ? (
          <ErrorNotice error={settings.error} title="The app cannot load the upload settings" onRetry={() => void settings.refetch()} />
        ) : s ? (
          <>
            <KeyValues
              items={[
                ["Uploads", s.uploads_allowed ? "Allowed" : "Not allowed"],
                ...(s.reason ? ([["Why", s.reason]] as [string, string][]) : []),
                ["Tenant code", s.tenant_code ? <Mono key="c">{s.tenant_code}</Mono> : <span key="c" className="text-muted-foreground">Assigned with the first plugin</span>],
              ]}
            />
            <div className="flex items-start gap-3 border-t pt-4">
              <Switch
                id="plugin-private-use"
                data-testid="plugin-private-use"
                className="mt-0.5"
                checked={s.private_use}
                disabled={update.isPending}
                aria-describedby="plugin-private-use-hint"
                onCheckedChange={(on) =>
                  update.mutate(
                    { private_use: on },
                    {
                      onSuccess: () =>
                        toast.success(on ? "Private use is on" : "Private use is off", on ? "Uploaders can use their own tested code before a review." : "Only approved uploads can run now."),
                      onError: (err) => toast.error("The app could not change the setting", errorMessage(err)),
                    },
                  )
                }
              />
              <div className="grid gap-0.5">
                <label htmlFor="plugin-private-use" className="text-sm font-medium">
                  Let uploaders use their own code before a review
                </label>
                <p id="plugin-private-use-hint" className="text-xs text-muted-foreground">
                  When this is on, the person who uploaded a version can run it as soon as its tests pass. Nobody else can until an admin approves it. Turn it off and only approved uploads run.
                </p>
              </div>
            </div>
          </>
        ) : null}
        <div className="border-t pt-4">
          <UploadersList tenantId={tenantId} />
        </div>
      </CardContent>
    </Card>
  );
}
