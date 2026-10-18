/**
 * M4: what an inspect run found in an upload (its groups and arrays, from metadata only),
 * and the raw HDF5 mapper that turns arrays into variables of tree nodes. Every name and
 * attribute in an inspection comes from the uploaded file: it renders as text only.
 */
import { AlertTriangle } from "lucide-react";

import type { InspectedArray, Role, UploadInspection } from "@/api/types";
import { Mono } from "@/components/common";
import { Checkbox } from "@/components/ui/overlays";
import { Badge, Input, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { ROLES, splitDims, type CoordSource, type Hdf5CoordDraft, type Hdf5Problem, type Hdf5VarDraft } from "./mapping";
import { ROOT } from "./tree";

function shapeText(shape: number[] | undefined): string {
  return shape?.length ? shape.map((n) => n.toLocaleString("en")).join(" × ") : "scalar";
}

/** The groups, arrays and warnings of an inspection. With `selected`, each array has a "map" checkbox. */
export function ArrayBrowser({
  inspection,
  file,
  expert,
  selected,
  onToggle,
}: {
  inspection: UploadInspection;
  file: string;
  expert: boolean;
  selected?: ReadonlySet<string>;
  onToggle?: (path: string, on: boolean) => void;
}) {
  const arrays = inspection.arrays ?? [];
  const groups = (inspection.groups ?? []).filter((g) => g !== ROOT);
  return (
    <div className="grid gap-2">
      <p className="text-xs text-muted-foreground">
        {arrays.length.toLocaleString("en")} {arrays.length === 1 ? "array" : "arrays"} in {Math.max(groups.length, 0).toLocaleString("en")} {groups.length === 1 ? "group" : "groups"} below the top level. The sandbox read only the metadata of the file, not its values.
      </p>
      {inspection.truncated ? (
        <p role="status" className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-xs">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" />
          The file has more arrays than the list can show. Only the first {arrays.length.toLocaleString("en")} arrays are in the list. You can map only these arrays.
        </p>
      ) : null}
      {inspection.warnings?.length ? (
        <ul aria-label="Inspection warnings" className="grid gap-0.5 text-xs text-warning">
          {inspection.warnings.map((w, i) => (
            <li key={i} className="break-words">{w}</li>
          ))}
        </ul>
      ) : null}
      <div className="max-h-72 overflow-auto rounded-lg border">
        <table aria-label={`Arrays of ${file}`} className="w-full text-[13px]">
          <thead className="sticky top-0 bg-card">
            <tr className="border-b text-xs text-muted-foreground">
              {selected ? <th className="w-8 px-2 py-2 text-left font-medium"><span className="sr-only">Map</span></th> : null}
              <th className="px-2 py-2 text-left font-medium">Path</th>
              <th className="px-2 py-2 text-left font-medium">Shape</th>
              <th className="px-2 py-2 text-left font-medium">Type</th>
              <th className="px-2 py-2 text-left font-medium">Dim names in the file</th>
              <th className="px-2 py-2 text-left font-medium">Filters</th>
              {expert ? <th className="px-2 py-2 text-left font-medium">Chunks</th> : null}
            </tr>
          </thead>
          <tbody>
            {arrays.map((a) => (
              <tr key={a.path} className={cn("border-b last:border-0", selected?.has(a.path) && "bg-accent/50")}>
                {selected ? (
                  <td className="px-2 py-1">
                    <Checkbox checked={selected.has(a.path)} onCheckedChange={(c) => onToggle?.(a.path, c === true)} aria-label={`Map ${a.path}`} />
                  </td>
                ) : null}
                <td className="px-2 py-1 break-all"><Mono>{a.path}</Mono></td>
                <td className="px-2 py-1 whitespace-nowrap tabular">{shapeText(a.shape)}</td>
                <td className="px-2 py-1"><Mono>{a.dtype}</Mono></td>
                <td className="px-2 py-1">{a.dims_hint?.length ? <Mono>({a.dims_hint.join(", ")})</Mono> : <span className="text-muted-foreground">none</span>}</td>
                <td className="px-2 py-1">
                  <span className="flex flex-wrap gap-1">
                    {(a.filters ?? []).length ? (a.filters ?? []).map((f) => <Badge key={f} variant="outline">{f}</Badge>) : <span className="text-muted-foreground">none</span>}
                  </span>
                </td>
                {expert ? <td className="px-2 py-1 tabular text-muted-foreground">{a.chunks?.length ? a.chunks.join(" × ") : "contiguous"}</td> : null}
              </tr>
            ))}
            {!arrays.length ? (
              <tr>
                <td colSpan={7} className="px-2 py-3 text-muted-foreground">The file has no arrays.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** 1-D arrays of the file: the label arrays a coordinate can read. */
function labelArrays(arrays: InspectedArray[]): InspectedArray[] {
  return arrays.filter((a) => (a.shape ?? []).length === 1);
}

/**
 * The raw HDF5 mapper: each selected array becomes a variable (name, node, one dim name per
 * axis, role, units); each dim gets a coordinate (an index, a 1-D array of labels or a range).
 */
export function Hdf5Mapper({
  inspection,
  vars,
  coords,
  onVar,
  onCoord,
  problems,
  expert,
}: {
  inspection: UploadInspection;
  vars: Hdf5VarDraft[];
  coords: Hdf5CoordDraft[];
  onVar: (path: string, patch: Partial<Hdf5VarDraft>) => void;
  onCoord: (dim: string, node: string, patch: Partial<Hdf5CoordDraft>) => void;
  problems: Hdf5Problem[];
  expert: boolean;
}) {
  const mapped = vars.filter((v) => v.include);
  const labels = labelArrays(inspection.arrays ?? []);
  const flagged = new Set(problems.map((p) => p.key));
  return (
    <div className="grid gap-4">
      {mapped.length ? (
        <div className="grid gap-2">
          <h4 className="text-[13px] font-semibold">Variables</h4>
          {!expert ? (
            <p className="text-xs text-muted-foreground">
              Give each array a variable name. Type one dim name for each axis, separated by commas (for example <Mono>row, col</Mono>). Arrays with the same dim name must have the same size on that axis. The node is the place of the variable in the tree: / is the top.
            </p>
          ) : null}
          <div className="overflow-x-auto rounded-lg border">
            <table aria-label="Variables from the arrays" className="w-full text-[13px]">
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th className="px-2 py-2 text-left font-medium">Array</th>
                  <th className="px-2 py-2 text-left font-medium">Variable</th>
                  <th className="px-2 py-2 text-left font-medium">Node</th>
                  <th className="px-2 py-2 text-left font-medium">Dims</th>
                  <th className="px-2 py-2 text-left font-medium">Role and units</th>
                </tr>
              </thead>
              <tbody>
                {mapped.map((v) => (
                  <tr key={v.path} className="border-b align-top last:border-0">
                    <td className="px-2 py-1.5">
                      <Mono className="break-all">{v.path}</Mono>
                      <span className="block text-xs text-muted-foreground tabular">{shapeText(v.shape)}</span>
                    </td>
                    <td className="px-2 py-1.5">
                      <Input aria-label={`Variable name for ${v.path}`} value={v.name} onChange={(e) => onVar(v.path, { name: e.target.value })} aria-invalid={flagged.has(v.path)} className="h-8 w-36 font-mono" />
                    </td>
                    <td className="px-2 py-1.5">
                      <Input aria-label={`Node for ${v.path}`} value={v.node} onChange={(e) => onVar(v.path, { node: e.target.value })} className="h-8 w-32 font-mono" placeholder="/" />
                    </td>
                    <td className="px-2 py-1.5">
                      <Input aria-label={`Dims for ${v.path}`} value={v.dims} onChange={(e) => onVar(v.path, { dims: e.target.value })} className="h-8 w-40 font-mono" placeholder={v.shape.map((_, i) => `dim_${i}`).join(", ")} />
                      <span className="block text-xs text-muted-foreground">
                        {splitDims(v.dims).length} of {v.shape.length} {v.shape.length === 1 ? "axis" : "axes"}
                      </span>
                    </td>
                    <td className="px-2 py-1.5">
                      <div className="flex flex-wrap gap-1.5">
                        <NativeSelect aria-label={`Role for ${v.path}`} value={v.role} onChange={(e) => onVar(v.path, { role: e.target.value as Role })} className="h-8 w-32">
                          {ROLES.map((r) => <option key={r}>{r}</option>)}
                        </NativeSelect>
                        <Input aria-label={`Units for ${v.path}`} placeholder="units" value={v.units} onChange={(e) => onVar(v.path, { units: e.target.value })} className="h-8 w-24" />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
      {coords.length ? (
        <div className="grid gap-2">
          <h4 className="text-[13px] font-semibold">Coordinates</h4>
          {!expert ? (
            <p className="text-xs text-muted-foreground">
              A coordinate gives the labels of a dim. Use positions 0, 1, 2… (index), the values of a 1-D array of the file, or a range: a first value and a step (numbers, or a date such as 2024-01-01 and a frequency such as W-MON).
            </p>
          ) : null}
          <div className="overflow-x-auto rounded-lg border">
            <table aria-label="Coordinates of the dims" className="w-full text-[13px]">
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th className="px-2 py-2 text-left font-medium">Dim</th>
                  <th className="px-2 py-2 text-left font-medium">Labels from</th>
                  <th className="px-2 py-2 text-left font-medium">Details</th>
                </tr>
              </thead>
              <tbody>
                {coords.map((c) => (
                  <tr key={`${c.node}:${c.dim}`} className="border-b align-top last:border-0">
                    <td className="px-2 py-1.5">
                      <Mono>{c.dim}</Mono>
                      {c.node !== ROOT ? <span className="block text-xs text-muted-foreground">node <Mono>{c.node}</Mono></span> : null}
                    </td>
                    <td className="px-2 py-1.5">
                      <NativeSelect aria-label={`Labels of ${c.dim}`} value={c.source} onChange={(e) => onCoord(c.dim, c.node, { source: e.target.value as CoordSource })} className="h-8 w-40" aria-invalid={flagged.has(`coord:${c.node}:${c.dim}`)}>
                        <option value="index">index (0, 1, 2…)</option>
                        <option value="path">an array of the file</option>
                        <option value="range">a range</option>
                      </NativeSelect>
                    </td>
                    <td className="px-2 py-1.5">
                      {c.source === "path" ? (
                        <NativeSelect aria-label={`Label array for ${c.dim}`} value={c.path} onChange={(e) => onCoord(c.dim, c.node, { path: e.target.value })} className="h-8 w-56">
                          <option value="">Select a 1-D array</option>
                          {labels.map((a) => (
                            <option key={a.path} value={a.path}>
                              {a.path} ({(a.shape ?? [])[0]} values)
                            </option>
                          ))}
                        </NativeSelect>
                      ) : c.source === "range" ? (
                        <div className="flex flex-wrap gap-1.5">
                          <Input aria-label={`Range start for ${c.dim}`} placeholder="start: 0 or 2024-01-01" value={c.start} onChange={(e) => onCoord(c.dim, c.node, { start: e.target.value })} className="h-8 w-40 font-mono" />
                          <Input aria-label={`Range step for ${c.dim}`} placeholder="step: 1 or W-MON" value={c.step} onChange={(e) => onCoord(c.dim, c.node, { step: e.target.value })} className="h-8 w-32 font-mono" />
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">positions 0, 1, 2…</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}
