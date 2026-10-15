import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowUp, Search } from "lucide-react";
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "@/lib/utils";

import { Input } from "./ui/primitives";

export interface DataTableProps<T> {
  data: T[];
  columns: ColumnDef<T, unknown>[];
  getRowId?: (row: T, index: number) => string;
  onRowClick?: (row: T) => void;
  /** Rendered when there are no rows (after filtering). */
  empty?: ReactNode;
  /** Show a text filter over all columns. */
  filterPlaceholder?: string;
  /** Max height of the scroll area; rows are virtualised inside it. */
  maxHeight?: number;
  rowHeight?: number;
  className?: string;
  initialSorting?: SortingState;
  label: string;
}

/**
 * Sortable, filterable, virtualised table (TanStack Table + Virtual). Rows are keyboard
 * reachable when `onRowClick` is set (Enter/Space activate).
 */
export function DataTable<T>({
  data,
  columns,
  getRowId,
  onRowClick,
  empty,
  filterPlaceholder,
  maxHeight = 560,
  rowHeight = 38,
  className,
  initialSorting = [],
  label,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const [filter, setFilter] = useState("");
  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter: filter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    ...(getRowId ? { getRowId } : {}),
  });
  const rows = table.getRowModel().rows;
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  });
  const items = virtualizer.getVirtualItems();
  const virtual = rows.length > 60 && items.length > 0;
  const padTop = virtual ? (items[0]?.start ?? 0) : 0;
  const padBottom = virtual ? virtualizer.getTotalSize() - (items.at(-1)?.end ?? 0) : 0;
  const visible = virtual ? items.map((v) => rows[v.index]).filter((r) => r !== undefined) : rows;
  const colCount = table.getVisibleLeafColumns().length;

  return (
    <div className={cn("grid gap-3", className)}>
      {filterPlaceholder ? (
        <div className="relative max-w-xs">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
          <Input
            aria-label={filterPlaceholder}
            placeholder={filterPlaceholder}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="pl-8"
          />
        </div>
      ) : null}
      <div ref={scrollRef} className="overflow-auto rounded-lg border bg-card" style={{ maxHeight }}>
        <table className="w-full text-[13px]" aria-label={label} aria-rowcount={rows.length}>
          <thead className="sticky top-0 z-[1] bg-card">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b">
                {hg.headers.map((h) => {
                  const sorted = h.column.getIsSorted();
                  const canSort = h.column.getCanSort();
                  return (
                    <th
                      key={h.id}
                      scope="col"
                      aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                      className="h-9 px-3 text-left text-xs font-medium whitespace-nowrap text-muted-foreground"
                    >
                      {h.isPlaceholder ? null : canSort ? (
                        <button
                          type="button"
                          className="inline-flex items-center gap-1 hover:text-foreground"
                          onClick={h.column.getToggleSortingHandler()}
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          {sorted === "asc" ? <ArrowUp className="size-3" /> : sorted === "desc" ? <ArrowDown className="size-3" /> : null}
                        </button>
                      ) : (
                        flexRender(h.column.columnDef.header, h.getContext())
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {padTop > 0 ? (
              <tr aria-hidden>
                <td colSpan={colCount} style={{ height: padTop }} />
              </tr>
            ) : null}
            {visible.map((row) => (
              <tr
                key={row.id}
                className={cn("border-b last:border-0", onRowClick && "cursor-pointer hover:bg-accent/60 focus-visible:bg-accent")}
                style={{ height: rowHeight }}
                {...(onRowClick
                  ? {
                      tabIndex: 0,
                      onClick: () => onRowClick(row.original),
                      onKeyDown: (e: KeyboardEvent) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onRowClick(row.original);
                        }
                      },
                    }
                  : {})}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-1.5 align-middle">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
            {padBottom > 0 ? (
              <tr aria-hidden>
                <td colSpan={colCount} style={{ height: padBottom }} />
              </tr>
            ) : null}
          </tbody>
        </table>
        {rows.length === 0 ? <div className="p-6 text-muted-foreground">{empty ?? "Nothing to show."}</div> : null}
      </div>
    </div>
  );
}
