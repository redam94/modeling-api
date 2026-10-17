/**
 * A secret the API shows exactly once (a webhook signing secret, an embed link's token): kept
 * in this dialog's props only, copied with one click, and dropped when the dialog closes.
 */
import { useState, type ReactNode } from "react";

import { CopyButton } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Label } from "@/components/ui/primitives";
import { toast } from "@/stores/toast";

export function SecretField({ id, label, value, copyLabel, onCopied }: { id: string; label: string; value: string; copyLabel: string; onCopied?: () => void }) {
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex items-center gap-1 rounded-md border bg-muted/60 p-1 pl-3">
        <input
          id={id}
          readOnly
          value={value}
          onFocus={(e) => e.currentTarget.select()}
          className="min-w-0 flex-1 bg-transparent font-mono text-[13px] outline-none"
          spellCheck={false}
          autoComplete="off"
        />
        <CopyButton
          value={value}
          label={copyLabel}
          onCopied={() => {
            toast.success("Copied to the clipboard");
            onCopied?.();
          }}
        />
      </div>
    </div>
  );
}

export function ShownOnceDialog({
  title,
  description,
  label,
  value,
  copyLabel,
  children,
  acknowledge = "I put it in a safe location",
  onClose,
}: {
  title: string;
  description: ReactNode;
  label: string;
  value: string;
  copyLabel: string;
  children?: ReactNode;
  acknowledge?: string;
  onClose: () => void;
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const close = () => {
    if (acknowledged || window.confirm("Close the dialog? You did not copy the value, and the app cannot show it again.")) onClose();
  };
  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent wide onInteractOutside={(e) => e.preventDefault()}>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        <SecretField id="shown-once-value" label={label} value={value} copyLabel={copyLabel} onCopied={() => setAcknowledged(true)} />
        {children}
        <label className="flex items-center gap-2 text-[13px]">
          <Checkbox checked={acknowledged} onCheckedChange={(v) => setAcknowledged(v === true)} />
          {acknowledge}
        </label>
        <DialogFooter>
          <Button onClick={onClose} disabled={!acknowledged}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
