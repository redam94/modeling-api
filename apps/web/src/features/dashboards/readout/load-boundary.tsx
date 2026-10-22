/**
 * A part of the readout that loads on demand (Try a case, Check accuracy, Score a file, the heavy
 * panels) behind its own error boundary: a chunk that no longer exists after a deploy, or a render
 * error on unexpected data, shows a plain message in that part only, and the rest of the readout
 * (and the share page) stays on screen.
 */
import { Component, Suspense } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";

import { Working } from "./parts";

export const LOAD_FAILED = "This part did not load. Reload the page.";

interface BoundaryState {
  failed: boolean;
}

export class LoadBoundary extends Component<{ children: React.ReactNode; label?: string }, BoundaryState> {
  override state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: unknown): void {
    console.warn("a readout part did not load", error);
  }

  override render(): React.ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" data-readout="load-failed" className="grid gap-2 rounded-lg border border-dashed p-4 text-sm">
        <p>{this.props.label ? `${this.props.label}: ` : ""}{LOAD_FAILED}</p>
        <span className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => this.setState({ failed: false })}>
            Try again
          </Button>
          <Button size="sm" variant="ghost" onClick={() => window.location.reload()}>
            Reload the page
          </Button>
        </span>
      </div>
    );
  }
}

/** A lazy part: its own error boundary around its Suspense. */
export function LazyPart({ children, fallback, label }: { children: React.ReactNode; fallback?: React.ReactNode; label?: string }) {
  return (
    <LoadBoundary label={label}>
      <Suspense fallback={fallback ?? <Working>Loading…</Working>}>{children}</Suspense>
    </LoadBoundary>
  );
}
