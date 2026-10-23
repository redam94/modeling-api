/**
 * The chat transport of a signed-in caller (v0.20): an analyst or viewer on the readout route,
 * and a client user in the portal. It asks `GET` / `POST /v1/dashboards/{id}/chat` through the
 * typed `api` (session cookie, CSRF header, `X-Mapi-Project`). Chat is session-only on the
 * server: an API token there answers 403.
 *
 * `api/client.ts` is loaded on the first call (a dynamic import, as `memberClient` does), so a
 * module that only builds the transport never pulls the session middleware into its chunk.
 */
import type { ChatTransport } from "./types";

export interface MemberChatTransportOptions {
  /**
   * The project (`tenant/project`) the dashboard lives in. Default: the selected project (the
   * session middleware adds it); pin it when the page may outlive a project switch.
   */
  scopeKey?: string | null;
}

/** The chat transport of dashboard `dashboardId` for the signed-in caller. */
export function memberChatTransport(dashboardId: string, opts: MemberChatTransportOptions = {}): ChatTransport {
  const load = () => import("@/api/client");
  const path = { dashboard_id: dashboardId };
  const headers = async () => {
    const { projectHeaders } = await load();
    return opts.scopeKey ? projectHeaders(opts.scopeKey) : {};
  };
  return {
    async availability(signal) {
      const { api, unwrap } = await load();
      return unwrap(api.GET("/v1/dashboards/{dashboard_id}/chat", { params: { path }, headers: await headers(), signal }));
    },
    async send(req, signal) {
      const { api, unwrap } = await load();
      return unwrap(api.POST("/v1/dashboards/{dashboard_id}/chat", { params: { path }, headers: await headers(), body: req, signal }));
    },
  };
}
