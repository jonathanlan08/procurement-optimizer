import "./badges.css";

/** Active / inactive / archived indicator, shared by suppliers and parts.
 * Archived always wins visually — an archived-but-active row still reads as archived. */
export function StatusBadge({
  isActive,
  isArchived,
}: {
  isActive: boolean;
  isArchived: boolean;
}) {
  if (isArchived) {
    return <span className="badge badge--archived">Archived</span>;
  }
  if (isActive) {
    return <span className="badge badge--active">Active</span>;
  }
  return <span className="badge badge--inactive">Inactive</span>;
}
