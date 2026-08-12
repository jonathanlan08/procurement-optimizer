import { Link } from "react-router-dom";

/** Catch-all page for unknown routes (`path="*"` in App.tsx). Historically a
 * stand-in while feature pages landed phase by phase; every workspace is live
 * now, so the only thing left to say is "wrong address". */
export function PlaceholderPage({ title }: { title: string }) {
  return (
    <section>
      <h1 style={{ fontSize: 18, marginBottom: "var(--space-md)" }}>{title}</h1>
      <p style={{ color: "var(--color-muted-foreground)", margin: 0 }}>
        There is nothing at this address. Check the URL, or head back to the{" "}
        <Link to="/">Overview</Link>.
      </p>
    </section>
  );
}
