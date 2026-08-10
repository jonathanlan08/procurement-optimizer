/** Reusable dense data table — MASTER.md "Data-Dense Analytical Workspace":
 * sticky header, 36px rows, hover highlight (no zebra striping), sortable
 * headers, tabular-nums right-aligned numerics, skeleton loading, and a
 * designed empty/error state (never a blank table). Shared by the Suppliers
 * and Parts workspaces.
 *
 * Sorting is client-side over the currently-loaded page: neither
 * `GET /suppliers` nor `GET /parts` accepts a `sort` query parameter
 * (backend/src/app/api/v1/{suppliers,parts}.py list only `q`/filters/
 * `limit`/`offset`), so "sortable columns where the API supports sort"
 * resolves to sorting the visible page rather than inventing a server
 * contract that doesn't exist.
 */

import {
  type ColumnDef,
  type RowData,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { type KeyboardEvent, useState } from "react";
import { ChevronDownIcon, ChevronUpDownIcon, ChevronUpIcon } from "./icons";
import "./DataTable.css";

declare module "@tanstack/react-table" {
  interface ColumnMeta<TData extends RowData, TValue> {
    /** numeric columns: right-align + tabular-nums (MASTER.md numerics rule) */
    align?: "left" | "right";
    /** monospace rendering for identifiers (part numbers, codes) */
    mono?: boolean;
  }
}

export interface DataTableProps<T> {
  ariaLabel: string;
  columns: ColumnDef<T>[];
  data: T[];
  getRowId: (row: T) => string;
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string | undefined;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyDescription?: string | undefined;
  onRowClick?: (row: T) => void;
  rowClassName?: (row: T) => string | undefined;
  skeletonRowCount?: number;
}

export function DataTable<T>({
  ariaLabel,
  columns,
  data,
  getRowId,
  isLoading = false,
  isError = false,
  errorMessage = "Something went wrong loading this data.",
  onRetry,
  emptyTitle = "No results",
  emptyDescription = "Nothing matches the current filters.",
  onRowClick,
  rowClassName,
  skeletonRowCount = 8,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([]);

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId,
  });

  const columnCount = columns.length;
  const rows = table.getRowModel().rows;

  function handleRowKeyDown(e: KeyboardEvent<HTMLTableRowElement>, row: T) {
    if (!onRowClick) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onRowClick(row);
    }
  }

  return (
    <div className="data-table-wrap">
      <table className="data-table" aria-label={ariaLabel}>
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => {
                const align = header.column.columnDef.meta?.align ?? "left";
                const canSort = header.column.getCanSort();
                const sortDir = header.column.getIsSorted();
                return (
                  <th key={header.id} data-align={align}>
                    {header.isPlaceholder ? null : canSort ? (
                      <button
                        type="button"
                        className="data-table-sort-btn"
                        onClick={header.column.getToggleSortingHandler()}
                        aria-label={`Sort by ${header.column.id}`}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sortDir === "asc" ? (
                          <ChevronUpIcon />
                        ) : sortDir === "desc" ? (
                          <ChevronDownIcon />
                        ) : (
                          <ChevronUpDownIcon className="icon-muted" />
                        )}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {isLoading ? (
            Array.from({ length: skeletonRowCount }).map((_, i) => (
              <tr key={`skeleton-${i}`} aria-hidden="true">
                {columns.map((_col, j) => (
                  <td key={j}>
                    <span className="skeleton-bar" />
                  </td>
                ))}
              </tr>
            ))
          ) : isError ? (
            <tr>
              <td className="data-table-state-cell" colSpan={columnCount}>
                <div className="data-table-state data-table-state--error" role="alert">
                  <div className="data-table-state-title">Couldn&apos;t load data</div>
                  <div className="data-table-state-desc">{errorMessage}</div>
                  {onRetry && (
                    <button type="button" className="data-table-retry-btn" onClick={onRetry}>
                      Retry
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ) : rows.length === 0 ? (
            <tr>
              <td className="data-table-state-cell" colSpan={columnCount}>
                <div className="data-table-state">
                  <div className="data-table-state-title">{emptyTitle}</div>
                  <div className="data-table-state-desc">{emptyDescription}</div>
                </div>
              </td>
            </tr>
          ) : (
            rows.map((row) => {
              const extraClass = rowClassName?.(row.original) ?? "";
              const clickable = onRowClick ? "is-clickable" : "";
              const className = [clickable, extraClass].filter(Boolean).join(" ") || undefined;
              return (
                <tr
                  key={row.id}
                  className={className}
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  onKeyDown={onRowClick ? (e) => handleRowKeyDown(e, row.original) : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                >
                  {row.getVisibleCells().map((cell) => {
                    const meta = cell.column.columnDef.meta;
                    return (
                      <td key={cell.id} data-align={meta?.align} className={meta?.mono ? "mono" : undefined}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    );
                  })}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
