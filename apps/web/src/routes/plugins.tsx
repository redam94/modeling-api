/**
 * v0.19 uploaded code (D20): the upload page, a plugin's versions and one version (with the
 * reviewer panel). A lazy route chunk (`plugins-*`, ≤ 96 KiB): signed-in pages only.
 */
import { getRouteApi } from "@tanstack/react-router";

import { PluginPage as PluginView } from "@/features/plugins/plugin-page";
import { UploadPage } from "@/features/plugins/upload-page";
import { VersionPage } from "@/features/plugins/version-page";

const newRoute = getRouteApi("/plugins/new");
const pluginRoute = getRouteApi("/plugins/$pluginId");
const versionRoute = getRouteApi("/plugins/$pluginId/versions/$versionId");

export function PluginUploadPage() {
  const { plugin } = newRoute.useSearch();
  return <UploadPage key={plugin ?? "-"} initialPlugin={plugin} />;
}

export function PluginPage() {
  const { pluginId } = pluginRoute.useParams();
  return <PluginView pluginId={pluginId} />;
}

export function PluginVersionPage() {
  const { pluginId, versionId } = versionRoute.useParams();
  return <VersionPage pluginId={pluginId} versionId={versionId} />;
}
