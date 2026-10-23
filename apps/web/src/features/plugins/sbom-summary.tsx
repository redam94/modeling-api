/**
 * The SBOM summary (`PluginSbom`, D8): how many files and how big, the licence, and every
 * requirement with the version the base image already has (uploads add no dependencies). The
 * full file list (path, size, sha256) is one click away. File names come from the upload.
 */
import { CheckCircle2, XCircle } from "lucide-react";

import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

import { displayText, sbomSummary } from "./states";
import type { PluginSbom } from "./types";

const STAT = "grid gap-0.5 rounded-xl bg-muted px-4 py-3";

export function SbomSummary({ sbom }: { sbom: PluginSbom | null | undefined }) {
  const s = sbomSummary(sbom);
  if (!sbom || !s) return null;
  const files = sbom.files ?? [];
  return (
    <section data-testid="plugin-sbom" aria-labelledby="plugin-sbom-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 rounded-2xl border bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-sbom-title" className="font-display text-lg leading-tight font-bold">
          Software bill of materials
        </h3>
        <p className="text-sm text-muted-foreground">Every file in the wheel and every package it needs. Uploads use the packages the platform already has.</p>
      </div>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className={STAT}>
          <dt className="text-xs text-muted-foreground">Files</dt>
          <dd className="font-display text-xl font-bold tabular" data-testid="plugin-sbom-files">
            {s.files.toLocaleString()}
          </dd>
        </div>
        <div className={STAT}>
          <dt className="text-xs text-muted-foreground">Size</dt>
          <dd className="font-display text-xl font-bold tabular">{formatBytes(s.bytes)}</dd>
        </div>
        <div className={STAT}>
          <dt className="text-xs text-muted-foreground">Requirements</dt>
          <dd className="font-display text-xl font-bold tabular">{s.requirements.length}</dd>
        </div>
        <div className={STAT}>
          <dt className="text-xs text-muted-foreground">Licence</dt>
          <dd className="truncate text-sm font-semibold" title={s.license ? displayText(s.license) : undefined}>
            {s.license ? displayText(s.license) : "Not stated"}
          </dd>
        </div>
      </dl>
      {s.requirements.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[26rem] text-left text-sm">
            <caption className="sr-only">Requirements and the version the base image has</caption>
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="py-1.5 pr-3 font-semibold">Package</th>
                <th scope="col" className="py-1.5 pr-3 font-semibold">Asks for</th>
                <th scope="col" className="py-1.5 font-semibold">Base image has</th>
              </tr>
            </thead>
            <tbody>
              {s.requirements.map((r, i) => (
                <tr key={`${r.name}:${i}`} className="border-t">
                  <td className="py-1.5 pr-3 font-mono text-xs [overflow-wrap:anywhere]">{displayText(r.name)}</td>
                  <td className="py-1.5 pr-3 font-mono text-xs [overflow-wrap:anywhere]">
                    {displayText(r.specifier ?? "") || "any"}
                    {r.marker ? <span className="block text-muted-foreground">; {displayText(r.marker)}</span> : null}
                  </td>
                  <td className={cn("py-1.5 text-xs", r.locked_version ? "text-success" : "text-destructive")}>
                    <span className="inline-flex items-center gap-1">
                      {r.locked_version ? <CheckCircle2 aria-hidden className="size-3.5" /> : <XCircle aria-hidden className="size-3.5" />}
                      {r.locked_version ? <span className="font-mono">{displayText(r.locked_version)}</span> : "Not installed"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">The wheel needs no other packages.</p>
      )}
      {s.unlocked ? (
        <p className="text-sm text-destructive">
          {s.unlocked === 1 ? "One requirement is" : `${s.unlocked} requirements are`} not in the base image. Use a version the base image has, or remove it.
        </p>
      ) : null}
      {files.length ? (
        <details className="rounded-lg border bg-muted/20 px-4 py-2">
          <summary className="cursor-pointer py-1 text-sm font-semibold select-none">All {files.length.toLocaleString()} files</summary>
          <ul className="grid max-h-80 gap-0.5 overflow-auto pt-2 pb-1 font-mono text-xs" aria-label="Files in the wheel">
            {files.map((f, i) => (
              <li key={`${f.path}:${i}`} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-t py-1 first:border-t-0">
                <span className="[overflow-wrap:anywhere]" title={f.sha256}>
                  {displayText(f.path)}
                </span>
                <span className="text-muted-foreground tabular">{formatBytes(f.size)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
