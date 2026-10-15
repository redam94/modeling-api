/**
 * Mock-mode stand-in for the identity provider. In a real deployment `GET /auth/login` on the
 * API redirects to the IdP (or the server's development IdP locally), which redirects back to
 * `/auth/callback` and then to `return_to`. MSW cannot answer that cross-origin navigation, so
 * the mock build serves this page at `/auth/login` instead: pick an identity, and the mocked
 * BFF "sets the session" and sends the browser back.
 *
 * Only bundled in `--mode mock` (imported behind `env.mock`, a build-time constant).
 */
import { ArrowRight, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { safeReturnTo } from "@/api/session";
import { Spinner } from "@/components/common";
import { env } from "@/env";
import { Logo } from "@/features/auth/gate";
import { useApplyTheme } from "@/hooks/use-theme";

import { PERSONAS } from "./tenancy";

const ROLE_SUMMARY: Record<string, string> = {
  ana: "Admin of Acme Retail, runner in Globex Sandbox, platform operator",
  rui: "Runner in Acme Retail / Growth",
  vic: "Viewer in Acme Retail / Growth",
};

export function DevIdpPage() {
  useApplyTheme();
  const params = new URLSearchParams(window.location.search);
  const returnTo = safeReturnTo(params.get("return_to"));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const signInAs = async (persona: string) => {
    setBusy(persona);
    setError(null);
    try {
      const res = await fetch(`${env.authBase}/__mock/auth/login`, {
        method: "POST",
        headers: { "content-type": "application/json", "X-Requested-With": "mapi-web" },
        credentials: "include",
        body: JSON.stringify({ persona }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      window.location.assign(returnTo);
    } catch (e) {
      setBusy(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <main id="main" className="grid min-h-svh place-items-center bg-background px-4 py-10">
      <div className="grid w-full max-w-md gap-6 rounded-xl border bg-card p-6 shadow-sm">
        <div className="flex items-center gap-2">
          <Logo className="size-7" />
          <span className="font-semibold">Development identity provider</span>
        </div>
        <div className="grid gap-1">
          <h1 className="text-xl font-semibold tracking-tight">Choose who to sign in as</h1>
          <p className="text-[13px] text-muted-foreground">
            Mock mode only. No password: this page stands in for your organisation&apos;s sign-in. You will return to <code className="font-mono">{returnTo}</code>.
          </p>
        </div>
        <ul className="grid gap-2" aria-label="Identities">
          {Object.entries(PERSONAS).map(([id, p]) => (
            <li key={id}>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void signInAs(id)}
                className="flex w-full items-center gap-3 rounded-lg border p-3 text-left hover:bg-accent/60 focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60"
              >
                <span aria-hidden className="grid size-9 shrink-0 place-items-center rounded-full bg-primary/14 text-xs font-semibold text-primary">
                  {p.name
                    .split(" ")
                    .map((w) => w[0])
                    .join("")}
                </span>
                <span className="grid min-w-0 flex-1">
                  <span className="flex items-center gap-1.5 font-medium">
                    Sign in as {p.name}
                    {p.operator ? <ShieldCheck className="size-3.5 text-primary" aria-label="platform operator" /> : null}
                  </span>
                  <span className="truncate text-xs text-muted-foreground">
                    {p.email} · {ROLE_SUMMARY[id]}
                  </span>
                </span>
                {busy === id ? <Spinner /> : <ArrowRight className="size-4 text-muted-foreground" />}
              </button>
            </li>
          ))}
        </ul>
        {error ? (
          <p role="alert" className="text-[13px] text-destructive">
            Sign-in failed: {error}
          </p>
        ) : null}
      </div>
    </main>
  );
}
