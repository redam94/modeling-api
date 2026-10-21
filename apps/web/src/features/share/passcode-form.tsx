/**
 * The passcode step of a share link (`SHARE_PASSCODE_REQUIRED`). The passcode is sent with the
 * token in one POST body and is not kept after the session opens. Five wrong passcodes lock the
 * link for a while (`SHARE_LOCKED`, 429 with Retry-After).
 */
import { KeyRound, Loader2 } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/primitives";
import { MAX_PASSCODE_LEN, MIN_PASSCODE_LEN } from "@/features/dashboards/types";

/** A field error for `value`, or `null` when it may be sent. */
export function passcodeError(value: string): string | null {
  if (!value) return "Type the passcode that you got with the link.";
  if (value.length < MIN_PASSCODE_LEN) return `The passcode has at least ${MIN_PASSCODE_LEN} characters. Check what you typed.`;
  if (value.length > MAX_PASSCODE_LEN) return `The passcode has a maximum of ${MAX_PASSCODE_LEN} characters. Check what you typed.`;
  return null;
}

/** "Try again in 15 minutes." from a Retry-After in seconds. */
export function retryWords(seconds: number | null): string {
  if (seconds === null || seconds <= 0) return "Try again later.";
  if (seconds < 60) return "Try again in a minute.";
  const minutes = Math.ceil(seconds / 60);
  return `Try again in ${minutes} ${minutes === 1 ? "minute" : "minutes"}.`;
}

export interface PasscodeFormProps {
  busy: boolean;
  /** The server's answer to the last passcode (wrong, locked), in plain words. */
  error: string | null;
  /** The link is locked: the form cannot be sent. */
  locked?: boolean;
  onSubmit: (passcode: string) => void;
}

export function PasscodeForm({ busy, error, locked = false, onSubmit }: PasscodeFormProps) {
  const id = useId();
  const [value, setValue] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const problem = passcodeError(value);
    setFieldError(problem);
    if (problem || busy || locked) return;
    onSubmit(value);
  };

  return (
    <form className="grid gap-5" onSubmit={submit} noValidate data-share="passcode">
      <div className="grid gap-2">
        <div className="flex items-center gap-2.5">
          <KeyRound className="size-5 text-muted-foreground" aria-hidden />
          <h1 className="text-xl font-semibold tracking-tight">Type the passcode</h1>
        </div>
        <p className="text-base text-muted-foreground">The person who sent you this link protected it with a passcode. They sent the passcode to you separately.</p>
      </div>
      <Field label="Passcode" htmlFor={id} error={fieldError ?? undefined}>
        <Input
          id={id}
          type="password"
          autoComplete="off"
          autoFocus
          maxLength={MAX_PASSCODE_LEN}
          value={value}
          disabled={locked}
          onChange={(e) => {
            setValue(e.target.value);
            if (fieldError) setFieldError(null);
          }}
        />
      </Field>
      {error ? <InlineMessage tone={locked ? "warning" : "error"}>{error}</InlineMessage> : null}
      <Button type="submit" className="w-fit" disabled={busy || locked}>
        {busy ? <Loader2 className="animate-spin" aria-hidden /> : null}
        Open the dashboard
      </Button>
    </form>
  );
}
