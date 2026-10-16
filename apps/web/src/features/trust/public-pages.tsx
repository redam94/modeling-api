/**
 * The public trust pages (no session): self-service sign-up with its "check your inbox" state,
 * and the `/verify-email` landing the e-mail links to. Verification creates a `public` tier
 * workspace; the user then signs in through the usual identity-provider flow.
 *
 * The sign-up answer is the same for new and known addresses (it cannot be used to find
 * accounts), so the page never says whether an address already has a workspace.
 */
import { ArrowRight, CheckCircle2, Inbox, LogIn, MailWarning } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import { signIn } from "@/api/session";
import { signup, TERMS_VERSION, verifyEmail } from "@/api/trust";
import type { VerificationResult } from "@/api/types";
import { ErrorNotice, Mono, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Field, Input } from "@/components/ui/primitives";
import { env } from "@/env";
import { Logo } from "@/features/auth/gate";
import { useApplyTheme } from "@/hooks/use-theme";

import { VERIFY_TOKEN_RE, type PublicPath } from "./public-paths";

/** A new workspace opens in Guided mode: its first page answers "What do you want to learn?". */
const SIGNED_UP_LANDING = "/guided";
const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,255}$/;
const NAME_RE = /^[^\u0000-\u001f\u007f]+$/;

function PublicFrame({ children }: { children: ReactNode }) {
  return (
    <main id="main" className="grid min-h-svh place-items-center bg-background px-4 py-10">
      <div className="grid w-full max-w-md gap-6">
        <a href="/" className="flex w-fit items-center gap-2.5">
          <Logo className="size-8" />
          <span className="text-lg font-semibold tracking-tight">
            modeling<span className="text-muted-foreground">-api</span>
          </span>
        </a>
        <div className="grid gap-5 rounded-xl border bg-card p-6 shadow-sm">{children}</div>
      </div>
    </main>
  );
}

// ----------------------------------------------------------------------- sign-up

export interface SignupForm {
  email: string;
  name: string;
  organisation: string;
  terms: boolean;
}

export function validateSignup(f: SignupForm): Partial<Record<keyof SignupForm, string>> {
  const errors: Partial<Record<keyof SignupForm, string>> = {};
  const email = f.email.trim();
  if (!email) errors.email = "Enter your work e-mail address.";
  else if (email.length > 320 || !EMAIL_RE.test(email) || !email.split("@")[1]?.includes(".")) errors.email = "That does not look like an e-mail address.";
  const name = f.name.trim();
  if (!name) errors.name = "Tell us your name.";
  else if (name.length > 200 || !NAME_RE.test(name)) errors.name = "Up to 200 characters, no control characters.";
  if (f.organisation.trim().length > 200) errors.organisation = "Up to 200 characters.";
  if (!f.terms) errors.terms = "Accept the terms of service to create a workspace.";
  return errors;
}

/** Field errors from the server's problem (`pointer` → field), in plain words. */
export function signupProblemErrors(err: unknown): Partial<Record<keyof SignupForm, string>> {
  if (!(err instanceof ApiError) || !err.problem) return {};
  const p = err.problem;
  if (p.code === "EMAIL_NOT_ALLOWED") {
    const text = `${p.detail ?? ""} ${(p.errors ?? []).map((e) => e.message).join(" ")}`;
    return {
      email: /disposable/i.test(text)
        ? "Disposable e-mail addresses cannot sign up. Use your work address: it is where we send the link, and how your colleagues find your workspace."
        : "This e-mail address cannot sign up. Check it, or use your work address.",
    };
  }
  if (p.code === "TERMS_NOT_ACCEPTED") return { terms: "The terms changed since this page loaded. Reload it, read them and accept again." };
  return {};
}

function DevOutboxHint({ email }: { email: string }) {
  const [link, setLink] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    let tries = 0;
    const look = async () => {
      tries += 1;
      try {
        const res = await fetch(`${env.apiBase}/dev/outbox?to=${encodeURIComponent(email)}&limit=1`, { credentials: "omit", referrerPolicy: "no-referrer" });
        if (res.ok) {
          const body = (await res.json()) as { items?: { tags?: { token?: unknown } }[] };
          const token = body.items?.[0]?.tags?.token;
          if (typeof token === "string" && VERIFY_TOKEN_RE.test(token)) {
            if (!stop) setLink(`/verify-email?token=${token}`);
            return;
          }
        }
      } catch {
        // no development outbox on this server
      }
      if (!stop && tries < 8) setTimeout(() => void look(), 1500);
    };
    void look();
    return () => {
      stop = true;
    };
  }, [email]);
  if (!link) return null;
  return (
    <p className="rounded-md border border-dashed p-3 text-[13px]">
      Development server: the e-mail went to the outbox.{" "}
      <a href={link} className="font-medium text-primary hover:underline">
        Open the verification link
      </a>
    </p>
  );
}

export function SignupPage() {
  const [form, setForm] = useState<SignupForm>({ email: "", name: "", organisation: "", terms: false });
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [sent, setSent] = useState<{ email: string; hours: number } | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const local = validateSignup(form);
  const server = signupProblemErrors(error);
  const shown = { ...(submitted ? local : {}), ...server };
  const generic = error && !Object.keys(server).length ? error : null;
  const set = <K extends keyof SignupForm>(k: K, v: SignupForm[K]) => {
    setForm((f) => ({ ...f, [k]: v }));
    if (k === "email" || k === "terms") setError(null);
  };

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    setSubmitted(true);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      const accepted = await signup({
        email: form.email.trim(),
        name: form.name.trim(),
        accepted_terms: form.terms,
        terms_version: TERMS_VERSION,
        ...(form.organisation.trim() ? { organisation: form.organisation.trim() } : {}),
      });
      setSent({ email: form.email.trim(), hours: Math.round((accepted.expires_in_s ?? 86_400) / 3600) });
    } catch (err) {
      setError(err);
      if (signupProblemErrors(err).email) emailRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  if (sent) {
    return (
      <PublicFrame>
        <div className="grid gap-3" role="status" aria-live="polite">
          <Inbox className="size-8 text-primary" aria-hidden />
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight">Check your inbox</h1>
          <p className="text-muted-foreground">
            If <span className="font-medium text-foreground">{sent.email}</span> can sign up, a link to confirm it is on its way. It works once, for {sent.hours} hours, and creates your workspace.
          </p>
          <ul className="list-disc pl-5 text-[13px] text-muted-foreground">
            <li>Nothing yet? Look in the spam folder, or send it again: only the newest link works.</li>
            <li>Opened the link on another device? That is fine; sign in there or here afterwards.</li>
          </ul>
          {env.mock || import.meta.env.DEV ? <DevOutboxHint email={sent.email} /> : null}
          {generic ? <ErrorNotice error={generic} /> : null}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button variant="outline" disabled={busy} onClick={() => void submit()}>
              {busy ? <Spinner /> : null} Send the link again
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setSent(null);
                setSubmitted(false);
              }}
            >
              Use a different address
            </Button>
          </div>
        </div>
      </PublicFrame>
    );
  }

  return (
    <PublicFrame>
      <div className="grid gap-1.5">
        <h1 className="text-[22px] leading-tight font-semibold tracking-tight">Create your workspace</h1>
        <p className="text-[13px] text-muted-foreground">Free workspaces run the built-in models and declarative specs on your data. We e-mail you a link to confirm the address.</p>
      </div>
      <form className="grid gap-4" onSubmit={(e) => void submit(e)} noValidate>
        <Field label="Work e-mail" htmlFor="su-email" error={shown.email}>
          <Input ref={emailRef} id="su-email" type="email" autoComplete="email" autoFocus maxLength={320} value={form.email} onChange={(e) => set("email", e.target.value)} />
        </Field>
        <Field label="Your name" htmlFor="su-name" error={shown.name}>
          <Input id="su-name" autoComplete="name" maxLength={200} value={form.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Organisation (optional)" htmlFor="su-org" error={shown.organisation} hint="Names the workspace; you can invite colleagues later.">
          <Input id="su-org" autoComplete="organization" maxLength={200} value={form.organisation} onChange={(e) => set("organisation", e.target.value)} />
        </Field>
        <div className="grid gap-1.5">
          <label className="flex items-start gap-2 text-[13px]" htmlFor="su-terms">
            <Checkbox id="su-terms" className="mt-0.5" checked={form.terms} onCheckedChange={(v) => set("terms", v === true)} aria-invalid={shown.terms ? true : undefined} aria-describedby={shown.terms ? "su-terms-error" : undefined} />
            <span>
              I accept the terms of service and the acceptable-use policy (version <Mono>{TERMS_VERSION}</Mono>).
            </span>
          </label>
          {shown.terms ? (
            <p id="su-terms-error" role="alert" className="text-xs font-medium text-destructive">
              {shown.terms}
            </p>
          ) : null}
        </div>
        {generic ? <ErrorNotice error={generic} title={generic instanceof ApiError && generic.code === "FORBIDDEN" ? "Sign-up is closed on this server" : "Sign-up did not go through"} /> : null}
        <Button type="submit" size="lg" disabled={busy}>
          {busy ? <Spinner /> : <ArrowRight />} Create workspace
        </Button>
      </form>
      <p className="text-[13px] text-muted-foreground">
        Already have an account?{" "}
        <button type="button" className="font-medium text-primary hover:underline" onClick={() => signIn("/")}>
          Sign in
        </button>
      </p>
    </PublicFrame>
  );
}

// ----------------------------------------------------------------------- verify

/** Read `?token=` once and take it out of the address bar (history, Referer, screenshots). */
function useTokenFromUrl(): string | null {
  const [token] = useState(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("token");
  });
  useEffect(() => {
    if (window.location.search) window.history.replaceState(null, "", window.location.pathname);
  }, []);
  return token;
}

export function VerifyEmailPage() {
  const token = useTokenFromUrl();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<VerificationResult | null>(null);
  const valid = token !== null && VERIFY_TOKEN_RE.test(token);

  if (result) {
    return (
      <PublicFrame>
        <div className="grid gap-3" role="status">
          <CheckCircle2 className="size-8 text-success" aria-hidden />
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight">Your workspace is ready</h1>
          <p className="text-muted-foreground">
            <span className="font-medium text-foreground">{result.email}</span> is confirmed. Workspace <Mono>{result.tenant_id}</Mono> has a <Mono>{result.project_id}</Mono> project, and you are its admin.
          </p>
          <p className="text-[13px] text-muted-foreground">Free workspaces run the built-in models and declarative specs. Uploading your own model code needs identity verification.</p>
          <Button size="lg" onClick={() => signIn(SIGNED_UP_LANDING, { login_url: result.login_url ?? "/auth/login" })}>
            <LogIn /> Sign in
          </Button>
        </div>
      </PublicFrame>
    );
  }

  const expired = error instanceof ApiError && error.code === "LINK_EXPIRED";
  return (
    <PublicFrame>
      {!valid ? (
        <div className="grid gap-3">
          <MailWarning className="size-8 text-warning" aria-hidden />
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight">This link is incomplete</h1>
          <p className="text-muted-foreground">Open the link from the e-mail exactly as it arrived, or ask for a new one.</p>
          <Button asChild variant="outline">
            <a href="/signup">Sign up again</a>
          </Button>
        </div>
      ) : expired ? (
        <div className="grid gap-3" role="alert">
          <MailWarning className="size-8 text-warning" aria-hidden />
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight">This link has expired</h1>
          <p className="text-muted-foreground">Links work once and for 24 hours, and only the newest one sent to an address. If you already confirmed it, just sign in.</p>
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => signIn("/")}>
              <LogIn /> Sign in
            </Button>
            <Button asChild variant="outline">
              <a href="/signup">Get a new link</a>
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-3">
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight">Confirm your e-mail address</h1>
          <p className="text-muted-foreground">One click creates your workspace and makes you its admin.</p>
          {error ? <ErrorNotice error={error} title="The address could not be confirmed" /> : null}
          <Button
            size="lg"
            disabled={busy}
            onClick={async () => {
              if (!token) return;
              setBusy(true);
              setError(null);
              try {
                setResult(await verifyEmail(token));
              } catch (e) {
                setError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? <Spinner /> : <CheckCircle2 />} Confirm and create my workspace
          </Button>
        </div>
      )}
    </PublicFrame>
  );
}

export function PublicApp({ path }: { path: PublicPath }) {
  useApplyTheme();
  return path === "/signup" ? <SignupPage /> : <VerifyEmailPage />;
}
