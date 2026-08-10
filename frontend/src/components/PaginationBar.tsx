import "./workspace.css";

export interface PaginationBarProps {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
}

/** Offset/limit pagination footer, driven by the API's `page: {limit, offset, total}` envelope. */
export function PaginationBar({ total, limit, offset, onOffsetChange }: PaginationBarProps) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  return (
    <div className="pagination-bar">
      <span>{total === 0 ? "No results" : `${from}–${to} of ${total}`}</span>
      <div className="pagination-controls">
        <button
          type="button"
          className="btn-ghost-sm"
          disabled={!canPrev}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <button
          type="button"
          className="btn-ghost-sm"
          disabled={!canNext}
          onClick={() => onOffsetChange(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
