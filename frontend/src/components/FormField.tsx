import type { ReactNode } from "react";
import "./workspace.css";

export interface FormFieldProps {
  label: string;
  error?: string | undefined;
  hint?: string;
  full?: boolean;
  checkbox?: boolean;
  children: ReactNode;
}

/** Label + control + validation message. Implicit `<label>` wrapping keeps
 * the control accessible by name without threading id/htmlFor by hand. */
export function FormField({ label, error, hint, full, checkbox, children }: FormFieldProps) {
  const classes = ["field", full && "field--full", checkbox && "field-checkbox"]
    .filter(Boolean)
    .join(" ");
  return (
    <label className={classes}>
      {checkbox ? (
        <>
          {children}
          <span className="field-label">{label}</span>
        </>
      ) : (
        <>
          <span className="field-label">
            {label}
            {hint && <span className="field-optional"> {hint}</span>}
          </span>
          {children}
        </>
      )}
      {error && (
        <span className="field-error" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}
