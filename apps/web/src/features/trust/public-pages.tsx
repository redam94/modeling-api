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
              I accept the{" "}
              <a href={env.termsUrl} target="_blank" rel="noopener noreferrer" className="font-medium text-primary hover:underline">
                terms of service
              </a>{" "}
              and the{" "}
              <a href={env.acceptableUseUrl} target="_blank" rel="noopener noreferrer" className="font-medium text-primary hover:underline">
                acceptable-use policy
              </a>{" "}
              (version <Mono>{TERMS_VERSION}</Mono>; they open in a new tab).
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

// ----------------------------------------------------------------------- terms

/**
 * The default documents sign-up links to (an installation replaces them with
 * `VITE_TERMS_URL` and `VITE_ACCEPTABLE_USE_URL`). They describe what the platform itself does
 * and enforces, in plain words.
 */
function DocumentPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <main id="main" className="grid min-h-svh justify-items-center bg-background px-4 py-10">
      <article className="grid w-full max-w-2xl content-start gap-5">
        <a href="/" className="flex w-fit items-center gap-2.5">
          <Logo className="size-8" />
          <span className="text-lg font-semibold tracking-tight">
            modeling<span className="text-muted-foreground">-api</span>
          </span>
        </a>
        <div className="grid gap-1.5">
          <h1 className="text-[26px] leading-tight font-semibold tracking-tight">{title}</h1>
          <p className="text-[13px] text-muted-foreground">
            Version <Mono>{TERMS_VERSION}</Mono>. The organisation running this service may publish its own, fuller version; sign-up then links to that one.
          </p>
        </div>
        <div className="grid gap-4 text-[14px] leading-relaxed [&_h2]:text-[16px] [&_h2]:font-semibold [&_ul]:grid [&_ul]:list-disc [&_ul]:gap-1.5 [&_ul]:pl-5">{children}</div>
        <p className="border-t pt-4 text-[13px]">
          <a href="/signup" className="font-medium text-primary hover:underline">
            Back to sign-up
          </a>
        </p>
      </article>
    </main>
  );
}

function TermsPage() {
  return (
    <DocumentPage title="Terms of service">
      <section className="grid gap-2">
        <h2>Your workspace</h2>
        <ul>
          <li>Sign up with a work e-mail address you control and your real name; one free workspace per person or organisation.</li>
          <li>A free workspace runs the built-in models and declarative specs, within quotas that keep the service fair for everyone. Uploading your own model code needs identity verification first.</li>
          <li>You are responsible for what is done with your account and its API tokens. Keep tokens and webhook secrets private.</li>
        </ul>
      </section>
      <section className="grid gap-2">
        <h2>Your data</h2>
        <ul>
          <li>Your data stays yours. Upload only data you are allowed to use for this purpose.</li>
          <li>It is used to run the analyses you ask for and for nothing else. Platform operators see metadata (names, sizes, run states, usage), not its content.</li>
          <li>An organisation admin can export everything the workspace stored (Settings &gt; Data) and delete the workspace: after a 7-day grace period, its data is purged from every store.</li>
        </ul>
      </section>
      <section className="grid gap-2">
        <h2>The service</h2>
        <ul>
          <li>Results are statistical estimates with their uncertainty. Check them, and the sanity scorecard, before you base decisions on them.</li>
          <li>Free workspaces come as they are, without a guarantee of availability. Features and quotas may change.</li>
          <li>A workspace that breaks the acceptable-use policy can be suspended; you are told why.</li>
          <li>When these terms change, you are asked to accept the new version.</li>
        </ul>
      </section>
    </DocumentPage>
  );
}

function AcceptableUsePage() {
  return (
    <DocumentPage title="Acceptable-use policy">
      <section className="grid gap-2">
        <h2>Use the service for modelling</h2>
        <ul>
          <li>Run analyses, forecasts and scenarios on data you are allowed to use. No mining, password cracking or other computing unrelated to modelling.</li>
          <li>Stay within your quotas. Do not open several accounts or workspaces to get around them.</li>
        </ul>
      </section>
      <section className="grid gap-2">
        <h2>Leave the platform and other people alone</h2>
        <ul>
          <li>Do not try to break out of the sandbox, read other workspaces&apos; data, or probe or attack the platform, its network or anyone else (webhooks and embed links included).</li>
          <li>Do not upload malware, or content or personal data you have no right to process.</li>
          <li>Found a weakness? Report it to the organisation running this service instead of testing it further.</li>
        </ul>
      </section>
      <section className="grid gap-2">
        <h2>What happens otherwise</h2>
        <ul>
          <li>Runs can be stopped, tokens and links revoked, and workspaces suspended or deleted. Serious cases may be reported to the authorities.</li>
        </ul>
      </section>
    </DocumentPage>
  );
}

export function PublicApp({ path }: { path: PublicPath }) {
  useApplyTheme();
  if (path === "/terms") return <TermsPage />;
  if (path === "/acceptable-use") return <AcceptableUsePage />;
  return path === "/signup" ? <SignupPage /> : <VerifyEmailPage />;
}
