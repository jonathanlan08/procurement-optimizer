import { ApiError } from "../api/client";
import "./workspace.css";

/** Renders the API's single error envelope: `message` plus per-field
 * `details[]` (backend/src/app/schemas — every mutation route shares this
 * shape). Anything not an ApiError (network failure, etc.) gets a generic
 * fallback rather than leaking implementation detail. */
export function ApiErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  if (!(error instanceof ApiError)) {
    return (
      <div className="banner-error" role="alert">
        Something went wrong. Please try again.
      </div>
    );
  }
  return (
    <div className="banner-error" role="alert">
      <div>{error.message}</div>
      {error.body.details.length > 0 && (
        <ul>
          {error.body.details.map((d, i) => (
            <li key={`${d.field ?? "root"}-${i}`}>
              {d.field ? <strong>{d.field}: </strong> : null}
              {d.issue}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
