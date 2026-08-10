/** Temporary stand-in while feature pages land phase by phase. */

export function PlaceholderPage({ title }: { title: string }) {
  return (
    <section>
      <h1 style={{ fontSize: 18, marginBottom: "var(--space-md)" }}>{title}</h1>
      <p style={{ color: "var(--color-muted-foreground)", margin: 0 }}>
        This workspace is under construction; it will be wired to the live API in a
        later phase.
      </p>
    </section>
  );
}
