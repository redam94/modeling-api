/**
 * MSW emulation of the trust lifecycle (`mapi_spec.trust`) for mock mode, the smoke suite and
 * component tests: public sign-up with the disposable-domain check, the development outbox
 * (`GET /dev/outbox`), e-mail verification, identity verification, tenant deletion with its
 * grace period (the tenant is suspended meanwhile), GDPR exports and the operators' sign-up
 * review queue.
 *
 * Sign-ups and their links survive page loads in `sessionStorage` (`mapi.mock.outbox`), so the
 * link in a mock e-mail can be opened like a real one; everything else lives per page.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type {
  GdprExport,
  GdprExportRequest,
  Issue,
  Problem,
  SignupRequest,
  SignupReview,
  SignupReviewDecision,
  TenantDeletion,
  VerificationStatus,
  VerificationUpdate,
} from "@/api/types";
import { env } from "@/env";

import { artifactLink } from "./fixtures";
import { mockPersonaId, PERSONAS, type createTenancyState } from "./tenancy";

const API = env.apiBase;
const AUTH = env.authBase;
export const MOCK_OUTBOX_KEY = "mapi.mock.outbox";
const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz";
const DISPOSABLE = ["mailinator.com", "yopmail.com", "guerrillamail.com", "10minutemail.com", "trashmail.com", "tempmail.com"];
const GRACE_MS = 7 * 86_400_000;

type TenancyState = ReturnType<typeof createTenancyState>;

interface OutboxMail {
  to: string;
  subject: string;
  body: string;
  created_at: string;
  tags: { kind: string; token: string; link: string };
  /** Mock bookkeeping, not part of the outbox format. */
  signup: { name: string; organisation: string | null; used: boolean };
}

function problem(status: number, code: Problem["code"], title: string, detail?: string, errors: Issue[] = [], headers: Record<string, string> = {}) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json", ...headers } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
const iso = (t: number) => new Date(t).toISOString().replace(/\.\d+Z$/, "Z");

function randomToken(n: number): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => ALPHABET[b % 32]).join("");
}

function readOutbox(): OutboxMail[] {
  try {
    const raw = sessionStorage.getItem(MOCK_OUTBOX_KEY);
    return raw ? (JSON.parse(raw) as OutboxMail[]) : [];
  } catch {
    return [];
  }
}

function writeOutbox(mails: OutboxMail[]): void {
  try {
    sessionStorage.setItem(MOCK_OUTBOX_KEY, JSON.stringify(mails.slice(0, 50)));
  } catch {
    // storage unavailable: the outbox lives as long as the call
  }
}

function slug(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "workspace"
  );
}

function persona() {
  const id = mockPersonaId();
  return id ? (PERSONAS[id] ?? null) : null;
}

export interface TrustState {
  verification: Map<string, VerificationStatus>;
  deletions: Map<string, TenantDeletion>;
  exports: Map<string, GdprExport & { readyAt: number }>;
  signups: SignupReview[];
}

export function createTrustState(): TrustState {
  return {
    verification: new Map<string, VerificationStatus>([
      ["acme", { tenant_id: "acme", trust_tier: "verified", email_verified: true, identity_verified: true, method: "document", uploaded_code_allowed: true, verified_at: "2026-09-02T10:00:00Z", reason_plain: "Identity verified by document on 2026-09-02." }],
      ["globex", { tenant_id: "globex", trust_tier: "public", email_verified: true, identity_verified: false, method: "email", uploaded_code_allowed: false, verified_at: null, reason_plain: "Public workspaces run built-in models and declarative specs only; uploaded code needs identity verification and a verified tier." }],
      ["initech", { tenant_id: "initech", trust_tier: "internal", email_verified: true, identity_verified: true, method: "manual", uploaded_code_allowed: true, verified_at: "2026-08-02T08:00:00Z", reason_plain: "An internal tenant." }],
    ]),
    deletions: new Map(),
    exports: new Map(),
    signups: [
      { id: "sgn_01jaf3s1g2n3u4p5a6b7c8d9e0", email: "ops+1@globex.example", email_domain: "globex.example", name: "Gil Globex", organisation: "Globex Labs", state: "verified", review: "flagged", flags: ["same_device", "same_email"], reasons_plain: ["Several workspaces were created from the same browser", "This address already created several workspaces"], ip_hash: "9f2c…a1", device_hash: "4be0…77", tenant_id: "globex", created_at: "2026-10-15T07:40:00Z", expires_at: "2026-10-16T07:40:00Z", verified_at: "2026-10-15T07:44:00Z" },
      { id: "sgn_01jaf3s1g2n3u4p5a6b7c8d9e1", email: "promo@burst.example", email_domain: "burst.example", name: "Pat Promo", organisation: null, state: "pending", review: "flagged", flags: ["domain_burst"], reasons_plain: ["Many sign-ups from this e-mail domain in a short time"], ip_hash: "11aa…0c", device_hash: null, tenant_id: null, created_at: "2026-10-15T08:12:00Z", expires_at: "2026-10-16T08:12:00Z", verified_at: null },
      { id: "sgn_01jaf3s1g2n3u4p5a6b7c8d9e2", email: "sam@northwind.example", email_domain: "northwind.example", name: "Sam Northwind", organisation: "Northwind", state: "verified", review: "none", flags: [], reasons_plain: [], tenant_id: "northwind", created_at: "2026-10-14T15:00:00Z", expires_at: "2026-10-15T15:00:00Z", verified_at: "2026-10-14T15:03:00Z" },
    ],
  };
}

const STORES = ["database", "object_store", "queue", "logs", "webhooks"] as const;

export interface TrustDeps {
  tenancy: TenancyState;
  artifacts: Map<string, { bytes: Uint8Array; type: string }>;
}

export function createTrustHandlers(state: TrustState, deps: TrustDeps): HttpHandler[] {
  const tenantOf = (id: string) => deps.tenancy.tenants.find((t) => t.id === id) ?? null;
  const isTenantAdmin = (t: string) => persona()?.tenants[t] === "admin";
  const needOperator = () => {
    const p = persona();
    if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");
    return p.operator ? null : problem(403, "FORBIDDEN", "Not allowed", "platform operators only");
  };
  const deletionOf = (t: string) => {
    const d = state.deletions.get(t);
    if (d && d.state === "deleting") {
      // Each poll moves the purge along one store.
      const stores = d.stores ?? [];
      const next = stores.find((s) => s.state !== "done");
      if (next) Object.assign(next, { state: "done", items_done: next.items_total ?? 1, updated_at: iso(Date.now()) });
      if (stores.every((s) => s.state === "done")) Object.assign(d, { state: "purged", completed_at: iso(Date.now()) });
    }
    return d ?? null;
  };

  return [
    // ------------------------------------------------------------------ public sign-up
    http.post(`${AUTH}/auth/signup`, async ({ request }) => {
      const body = (await request.json()) as SignupRequest;
      if (!body.accepted_terms || (body.terms_version ?? "2026-10") !== "2026-10") {
        return problem(422, "TERMS_NOT_ACCEPTED", "The terms of service were not accepted", "accept the terms of service (2026-10) to sign up", [
          { code: "TERMS_NOT_ACCEPTED", pointer: "/accepted_terms", message: "accept the terms of service (2026-10) to sign up" },
        ]);
      }
      const email = body.email.trim().toLowerCase();
      const domain = email.split("@")[1] ?? "";
      if (!/^[^@\s]{1,64}@[^@\s]{1,255}$/.test(email) || !domain.includes(".")) {
        return problem(422, "EMAIL_NOT_ALLOWED", "This e-mail address cannot sign up", "this e-mail address cannot sign up", [{ code: "EMAIL_NOT_ALLOWED", pointer: "/email", message: "this e-mail address cannot sign up" }]);
      }
      if (DISPOSABLE.some((d) => domain === d || domain.endsWith(`.${d}`))) {
        return problem(422, "EMAIL_NOT_ALLOWED", "This e-mail address cannot sign up", "disposable e-mail addresses cannot sign up; use your work address", [
          { code: "EMAIL_NOT_ALLOWED", pointer: "/email", message: "disposable e-mail addresses cannot sign up; use your work address" },
        ]);
      }
      if (domain === "ratelimited.example") {
        return problem(429, "RATE_LIMITED", "Too many requests", "too many sign-ups from this e-mail domain; try again later", [], { "Retry-After": "120" });
      }
      const token = `mev_${randomToken(40)}`;
      const origin = typeof window === "undefined" ? "http://127.0.0.1:5173" : window.location.origin;
      const link = `${origin}/verify-email?token=${token}`;
      const mails = readOutbox().map((m) => (m.to === email ? { ...m, signup: { ...m.signup, used: true } } : m)); // only the newest link works
      mails.unshift({
        to: email,
        subject: "Confirm your e-mail address for modeling-api",
        body: `Hello ${body.name},\n\nOpen this link within 24 hours to confirm it and create the workspace:\n\n${link}\n`,
        created_at: iso(Date.now()),
        tags: { kind: "email_verification", token, link },
        signup: { name: body.name, organisation: body.organisation ?? null, used: false },
      });
      writeOutbox(mails);
      return HttpResponse.json({ status: "check_email", expires_in_s: 86_400 }, { status: 202 });
    }),
    http.get(`${API}/dev/outbox`, ({ request }) => {
      const url = new URL(request.url);
      const to = url.searchParams.get("to")?.trim().toLowerCase();
      const limit = Math.min(100, Number(url.searchParams.get("limit") ?? 20) || 20);
      return HttpResponse.json({ items: readOutbox().filter((m) => !to || m.to === to).slice(0, limit).map(({ signup: _s, ...m }) => m) });
    }),
    http.post(`${AUTH}/auth/verify-email`, async ({ request }) => {
      const { token } = (await request.json()) as { token?: string };
      if (!token || !/^mev_[0-9a-hjkmnp-tv-z]{40}$/.test(token)) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "the link is incomplete", [{ code: "SCHEMA_INVALID", pointer: "/token", message: "not a verification token" }]);
      }
      const mails = readOutbox();
      const mail = mails.find((m) => m.tags.token === token);
      if (!mail || mail.signup.used) return problem(410, "LINK_EXPIRED", "The link has expired or was revoked", "this link has expired or was already used; sign up again for a new one");
      mail.signup.used = true;
      writeOutbox(mails);
      const tenant = slug(mail.signup.organisation ?? mail.to.split("@")[0] ?? "workspace");
      return HttpResponse.json({ email: mail.to, tenant_id: tenant, project_id: "default", trust_tier: "public", login_url: "/auth/login" });
    }),

    // ------------------------------------------------------------------ verification
    http.get(`${API}/v1/tenants/:tenant/verification`, ({ params }) => {
      const v = state.verification.get(String(params.tenant));
      return v ? HttpResponse.json(v) : problem(404, "NOT_FOUND", "Not found", "no such tenant");
    }),
    http.put(`${API}/v1/admin/tenants/:tenant/verification`, async ({ params, request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const t = String(params.tenant);
      const current = state.verification.get(t);
      if (!current) return problem(404, "NOT_FOUND", "Not found", "no such tenant");
      const body = (await request.json()) as VerificationUpdate;
      const tier = tenantOf(t)?.trust_tier ?? current.trust_tier;
      const next: VerificationStatus = {
        ...current,
        identity_verified: body.identity_verified,
        method: body.method,
        verified_at: body.identity_verified ? iso(Date.now()) : null,
        uploaded_code_allowed: body.identity_verified && tier !== "public",
        reason_plain: body.identity_verified
          ? tier === "public"
            ? "Identity verified. Public workspaces still cannot upload code until cut line B."
            : `Identity verified (${body.method}).`
          : `Identity verification withdrawn: ${body.reason}`,
      };
      state.verification.set(t, next);
      return HttpResponse.json(next);
    }),

    // ------------------------------------------------------------------ deletion
    http.get(`${API}/v1/tenants/:tenant/deletion`, ({ params }) => {
      const t = String(params.tenant);
      const p = persona();
      if (!p?.operator && !isTenantAdmin(t)) return problem(403, "FORBIDDEN", "Not allowed", "only organisation admins see the deletion");
      const d = deletionOf(t);
      return d ? HttpResponse.json(d) : problem(404, "NOT_FOUND", "Not found", `no deletion of tenant ${t}`);
    }),
    http.post(`${API}/v1/tenants/:tenant/deletion`, async ({ params, request }) => {
      const t = String(params.tenant);
      if (!isTenantAdmin(t)) return problem(403, "FORBIDDEN", "Not allowed", "only organisation admins can delete the organisation");
      const body = (await request.json()) as { confirm: string; reason?: string | null };
      if (body.confirm !== t) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "confirm must repeat the tenant id", [{ code: "SCHEMA_INVALID", pointer: "/confirm", message: `type ${t} to confirm` }]);
      }
      const existing = state.deletions.get(t);
      if (existing && (existing.state === "scheduled" || existing.state === "deleting")) return problem(409, "CONFLICT", "Conflict", "a deletion is already scheduled");
      const at = Date.now();
      const d: TenantDeletion = {
        id: `del_01jaf3d1e2l3e4t5e6${randomToken(8)}`,
        tenant_id: t,
        state: "scheduled",
        requested_at: iso(at),
        requested_by: persona()?.id ?? null,
        purge_after: iso(at + GRACE_MS),
        stores: STORES.map((store) => ({ store, state: "pending", items_done: 0 })),
        retained: { audit: "Audit log, pseudonymised, kept for its legal retention", cost_ledger: "Amounts only, kept for tax law" },
      };
      state.deletions.set(t, d);
      const tenant = tenantOf(t);
      if (tenant) Object.assign(tenant, { status: "suspended", status_reason: `Deletion requested; the purge starts after ${d.purge_after.slice(0, 10)}` });
      return HttpResponse.json(d, { status: 202 });
    }),
    http.delete(`${API}/v1/tenants/:tenant/deletion`, ({ params }) => {
      const t = String(params.tenant);
      if (!isTenantAdmin(t)) return problem(403, "FORBIDDEN", "Not allowed", "only organisation admins can cancel the deletion");
      const d = state.deletions.get(t);
      if (!d) return problem(404, "NOT_FOUND", "Not found", "no deletion to cancel");
      if (d.state !== "scheduled") return problem(409, "CONFLICT", "Conflict", `a ${d.state} deletion cannot be cancelled`);
      Object.assign(d, { state: "cancelled", cancelled_at: iso(Date.now()) });
      const tenant = tenantOf(t);
      if (tenant) Object.assign(tenant, { status: "active", status_reason: null });
      return HttpResponse.json(d);
    }),
    http.post(`${API}/v1/admin/tenants/:tenant/deletion/retry`, ({ params }) => {
      const denied = needOperator();
      if (denied) return denied;
      const d = state.deletions.get(String(params.tenant));
      if (!d) return problem(404, "NOT_FOUND", "Not found", "no deletion");
      if (d.state !== "failed") return problem(409, "CONFLICT", "Conflict", `a ${d.state} deletion cannot be retried`);
      Object.assign(d, { state: "deleting", stores: (d.stores ?? []).map((s) => (s.state === "failed" ? { ...s, state: "pending", detail: null } : s)) });
      return HttpResponse.json(d, { status: 202 });
    }),

    // ------------------------------------------------------------------ GDPR export
    http.post(`${API}/v1/tenants/:tenant/exports`, async ({ params, request }) => {
      const t = String(params.tenant);
      if (!isTenantAdmin(t)) return problem(403, "FORBIDDEN", "Not allowed", "only organisation admins can export the organisation's data");
      const body = (await request.json()) as GdprExportRequest;
      const exp: GdprExport & { readyAt: number } = {
        id: `exp_${randomToken(26)}`,
        tenant_id: t,
        state: "queued",
        include: body.include ?? ["metadata", "datasets", "models", "audit", "usage"],
        requested_by: persona()?.id ?? null,
        created_at: iso(Date.now()),
        readyAt: Date.now() + 700,
      };
      state.exports.set(exp.id, exp);
      const { readyAt: _r, ...dto } = exp;
      return HttpResponse.json(dto, { status: 202 });
    }),
    http.get(`${API}/v1/tenants/:tenant/exports/:id`, ({ params }) => {
      const exp = state.exports.get(String(params.id));
      if (!exp || exp.tenant_id !== params.tenant) return problem(404, "NOT_FOUND", "Not found", "no such export");
      if (exp.state === "queued" && Date.now() >= exp.readyAt) {
        const name = `exports/${exp.id}.zip`;
        const bytes = new TextEncoder().encode("PK\u0005\u0006" + "\u0000".repeat(18));
        deps.artifacts.set(name, { bytes, type: "application/zip" });
        Object.assign(exp, { state: "ready", ready_at: iso(Date.now()), size_bytes: bytes.length, expires_at: iso(Date.now() + 15 * 60_000), download_url: artifactLink(name, bytes, "application/zip").url });
      }
      const { readyAt: _r, ...dto } = exp;
      return HttpResponse.json(dto);
    }),

    // ------------------------------------------------------------------ sign-up review queue
    http.get(`${API}/v1/admin/signups`, ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const review = new URL(request.url).searchParams.get("review");
      return page(state.signups.filter((s) => !review || s.review === review));
    }),
    http.post(`${API}/v1/admin/signups/:id/review`, async ({ params, request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const s = state.signups.find((x) => x.id === params.id);
      if (!s) return problem(404, "NOT_FOUND", "Not found", "no such sign-up");
      if (s.review !== "flagged") return problem(409, "CONFLICT", "Conflict", "only flagged sign-ups are reviewed");
      const body = (await request.json()) as SignupReviewDecision;
      Object.assign(s, { review: body.decision === "approve" ? "approved" : "rejected", review_reason: body.reason, reviewed_at: iso(Date.now()), reviewed_by: persona()?.id ?? null });
      if (body.decision === "reject" && s.tenant_id) {
        const tenant = tenantOf(s.tenant_id);
        if (tenant) Object.assign(tenant, { status: "suspended", status_reason: `Sign-up rejected: ${body.reason}` });
      }
      return HttpResponse.json(s);
    }),
  ];
}
