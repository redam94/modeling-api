/**
 * The "Connect a chat tool" button of a signed-in readout page (v0.20, plan I-9, D24). The route
 * passes it to `ReadoutPage` as its `connect` prop on the member readout route and in the client
 * portal; share links never show it (share-link MCP uses the link and its passcode, see the
 * guide). It opens {@link ConnectDialog}.
 */
import { Plug } from "lucide-react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";

import { ConnectDialog } from "./connect-dialog";

export function ConnectChatToolButton({ dashboardId, title, size = "sm" }: { dashboardId: string; title?: string | null; size?: "sm" | "default" }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  // The dialog is unmounted on close and Radix returns focus only to a Dialog.Trigger, so the
  // button takes focus back itself (keyboard users stay where they were).
  const close = () => {
    setOpen(false);
    requestAnimationFrame(() => button.current?.focus());
  };
  return (
    <>
      <Button ref={button} size={size} variant="secondary" data-testid="connect-chat-tool" onClick={() => setOpen(true)}>
        <Plug /> Connect a chat tool
      </Button>
      {open ? <ConnectDialog dashboardId={dashboardId} title={title} onClose={close} /> : null}
    </>
  );
}
