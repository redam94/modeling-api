/**
 * The facts the file check read from the wheel without running it (`PluginInspection`): the
 * distribution, version and tags, the top-level package, the member count and size, the Python
 * range and every entry point. The reviewer reads these first (D13). Every value came from the
 * upload: text only.
 */
import { KeyValues, Mono } from "@/components/common";
import { formatBytes } from "@/lib/format";

import { displayText } from "./states";
import type { PluginInspection } from "./types";

export function InspectFacts({ inspection, namespace }: { inspection: PluginInspection; namespace?: string | undefined }) {
  const entryPoints = inspection.entry_points ?? [];
  const topLevel = inspection.top_level ?? [];
  const topOk = namespace ? topLevel.length === 1 && topLevel[0] === namespace : true;
  return (
    <section data-testid="plugin-inspect-facts" aria-labelledby="plugin-facts-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 rounded-2xl border bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-facts-title" className="font-display text-lg leading-tight font-bold">
          What's in the file
        </h3>
        <p className="text-sm text-muted-foreground">We read these from the wheel without running any of it.</p>
      </div>
      <KeyValues
        items={[
          ["Distribution", inspection.dist ? <Mono key="d">{displayText(inspection.dist)}</Mono> : "—"],
          ["Version", inspection.version ? <Mono key="v">{displayText(inspection.version)}</Mono> : "—"],
          ["Wheel tags", (inspection.wheel_tags ?? []).length ? <Mono key="t">{(inspection.wheel_tags ?? []).map(displayText).join(", ")}</Mono> : "—"],
          [
            "Top-level package",
            <span key="top" className="grid gap-0.5">
              <Mono>{topLevel.length ? topLevel.map(displayText).join(", ") : "—"}</Mono>
              {!topOk ? <span className="text-xs text-destructive">The package must be {namespace} only.</span> : null}
            </span>,
          ],
          ["Files", `${(inspection.members ?? 0).toLocaleString()} (${formatBytes(inspection.uncompressed_bytes)} unpacked)`],
          ["Python", inspection.requires_python ? <Mono key="py">{displayText(inspection.requires_python)}</Mono> : "Any version"],
          ["Archive sha256", <Mono key="sha" className="[overflow-wrap:anywhere]">{inspection.archive_sha256}</Mono>],
        ]}
      />
      {entryPoints.length ? (
        <details className="group rounded-lg border bg-muted/20 px-4 py-2" open={entryPoints.length <= 8}>
          <summary className="cursor-pointer py-1 text-sm font-semibold select-none">
            Entry points <span className="font-normal text-muted-foreground">({entryPoints.length})</span>
          </summary>
          <div className="overflow-x-auto pt-2 pb-1">
            <table className="w-full min-w-[28rem] text-left text-xs">
              <caption className="sr-only">Entry points the wheel declares</caption>
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-semibold">Group</th>
                  <th scope="col" className="py-1 pr-3 font-semibold">Name</th>
                  <th scope="col" className="py-1 font-semibold">Target</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {entryPoints.map((e, i) => (
                  <tr key={`${e.group}:${e.name}:${i}`} className="border-t">
                    <td className="py-1 pr-3 [overflow-wrap:anywhere]">{displayText(e.group)}</td>
                    <td className="py-1 pr-3 [overflow-wrap:anywhere]">{displayText(e.name)}</td>
                    <td className="py-1 [overflow-wrap:anywhere]">{displayText(e.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
    </section>
  );
}
