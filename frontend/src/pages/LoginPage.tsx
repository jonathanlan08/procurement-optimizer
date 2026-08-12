import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/session";
import { useEntranceStagger } from "../lib/motion";
import "./login.css";

/** Demo credentials are intentionally public: the seed
 * (backend/src/app/seed/demo_dataset.py) creates them, the README documents
 * them, and every byte behind them is synthetic. Surfacing them here with
 * one-click sign-in is the front door doing its job for a portfolio demo —
 * a real multi-tenant deployment would gate this block on a demo-mode flag. */
const DEMO_ACCOUNTS = [
  {
    role: "Analyst",
    hint: "Full workflow — quotes, scenarios, briefs, reports.",
    email: "demo-analyst@meridianfab.example",
    password: "demo-analyst-2026",
  },
  {
    role: "Owner",
    hint: "Everything the analyst can, plus administration.",
    email: "demo-owner@meridianfab.example",
    password: "demo-owner-2026",
  },
  {
    role: "Viewer",
    hint: "Read-only — see the role enforced server-side.",
    email: "demo-viewer@meridianfab.example",
    password: "demo-viewer-2026",
  },
] as const;

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // Sanctioned motion pattern 2 (lib/motion.ts): the brand panel's lines,
  // the sign-in card, and the demo block fade up in sequence on arrival.
  const pageRef = useEntranceStagger<HTMLDivElement>(
    ".login-brand-inner > *, .login-card, .login-demo",
  );
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // "form" | role name

  async function signIn(asEmail: string, asPassword: string, busyKey: string) {
    setBusy(busyKey);
    setError(null);
    try {
      await login(asEmail, asPassword);
      const from = (location.state as { from?: { pathname: string } })?.from?.pathname;
      navigate(from ?? "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setBusy(null);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void signIn(email, password, "form");
  }

  return (
    <div className="login-page" ref={pageRef}>
      <aside className="login-brand" aria-hidden="true">
        <div className="login-brand-inner">
          <span className="login-brand-mark" />
          <h2 className="login-brand-name">Procurement Optimizer</h2>
          <p className="login-brand-thesis">
            The cheapest unit price is routinely not the cheapest purchase. Exact-decimal
            landed cost, honest allocation, and a negotiation brief that never invents a
            number.
          </p>
          <div className="login-hue-row">
            <span style={{ background: "var(--hue-suppliers)" }} />
            <span style={{ background: "var(--hue-parts)" }} />
            <span style={{ background: "var(--hue-rfqs)" }} />
            <span style={{ background: "var(--hue-compare)" }} />
            <span style={{ background: "var(--hue-reports)" }} />
            <span style={{ background: "var(--hue-audit)" }} />
          </div>
          <p className="login-brand-footnote">Demo — synthetic data throughout.</p>
        </div>
      </aside>

      <main className="login-main">
        <form className="login-card" onSubmit={onSubmit}>
          <h1 className="login-title">Sign in</h1>
          <p className="login-subtitle">
            Landed-cost comparison, vendor scoring, and allocation optimization.
          </p>
          <label className="field">
            <span className="field-label">Email</span>
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="field">
            <span className="field-label">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && (
            <p className="login-error" role="alert">
              {error}
            </p>
          )}
          <button className="btn-primary" type="submit" disabled={busy !== null}>
            {busy === "form" ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <section className="login-demo" aria-label="Demo accounts">
          <h2 className="login-demo-title">Try the demo</h2>
          <p className="login-demo-note">
            Synthetic organization, public demo credentials — one click signs you in.
          </p>
          <ul className="login-demo-list">
            {DEMO_ACCOUNTS.map((acct) => (
              <li key={acct.role} className="login-demo-row">
                <div className="login-demo-meta">
                  <span className="login-demo-role">{acct.role}</span>
                  <span className="login-demo-hint">{acct.hint}</span>
                  <code className="login-demo-email">{acct.email}</code>
                </div>
                <button
                  type="button"
                  className="btn-demo"
                  disabled={busy !== null}
                  onClick={() => void signIn(acct.email, acct.password, acct.role)}
                >
                  {busy === acct.role ? "Signing in…" : `Sign in as ${acct.role}`}
                </button>
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
