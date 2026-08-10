import "./workspace.css";

export interface DetailRowProps {
  label: string;
  value: string;
  mono?: boolean;
  num?: boolean;
  full?: boolean;
}

/** Label/value pair for the drawer's read-only detail grid. */
export function DetailRow({ label, value, mono, num, full }: DetailRowProps) {
  const valueClass = ["detail-value", mono && "mono", num && "num"].filter(Boolean).join(" ");
  return (
    <div className={`detail-row${full ? " detail-row--full" : ""}`}>
      <span className="detail-label">{label}</span>
      <span className={valueClass}>{value}</span>
    </div>
  );
}
