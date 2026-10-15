import "./trusted-types";
import "@fontsource-variable/hanken-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "./app.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App, createQueryClient } from "./app";
import { env } from "./env";

async function start(): Promise<void> {
  if (env.mock) {
    const { startMockWorker } = await import("./mocks/browser");
    await startMockWorker();
  }
  const root = document.getElementById("root");
  if (!root) throw new Error("missing #root");
  if (env.mock && window.location.pathname === "/auth/login") {
    // The mock build's stand-in for the identity provider (a real server redirects to one).
    const { DevIdpPage } = await import("./mocks/dev-idp");
    createRoot(root).render(
      <StrictMode>
        <DevIdpPage />
      </StrictMode>,
    );
    return;
  }
  createRoot(root).render(
    <StrictMode>
      <App queryClient={createQueryClient()} />
    </StrictMode>,
  );
}

void start();
