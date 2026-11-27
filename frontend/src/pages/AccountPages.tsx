// Pages opened from email links (verify address, reset password) + "forgot password".
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button, ErrorBanner, inputClass } from "../components/ui";
import { AuthShell, Field, errorMessage } from "./AuthPages";

const INCOMPLETE_LINK = "This link is incomplete. Open the link from the email again.";

/**
 * Tokens arrive in the URL (?token=...). They are read ONCE and then removed
 * from the address bar, so they do not stay in the browser history or get
 * copied along with the URL. (Referrer-Policy: no-referrer already keeps them
 * from leaking to other sites.)
 */
function takeTokenFromUrl(path: string): string {
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  if (token) window.history.replaceState(null, "", path);
  return token;
}

function Success({ children }: { children: string }) {
  return (
    <p className="rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-800" role="status">{children}</p>
  );
}

const weakPassword = (p: string) => p.length > 0 && (p.length < 8 || !/[a-zA-Z]/.test(p) || !/\d/.test(p));

// React StrictMode runs effects twice in development: a token must be sent only once.
const submittedTokens = new Set<string>();

export function VerifyEmailPage() {
  const { user, reloadUser } = useAuth();
  const [token] = useState(() => takeTokenFromUrl("/verify-email"));
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(token ? null : INCOMPLETE_LINK);

  useEffect(() => {
    if (!token || submittedTokens.has(token)) return;
    submittedTokens.add(token);
    api.verifyEmail(token)
      .then(async () => {
        setDone(true);
        await reloadUser().catch(() => {}); // hides the "please confirm" banner
      })
      .catch((err) => setError(errorMessage(err)));
  }, [token, reloadUser]);

  return (
    <AuthShell title="Confirm your email" subtitle="One click to make sure we can reach you.">
      {!done && !error && <p className="text-sm text-ink-muted" role="status">Confirming…</p>}
      {done && <Success>Your email address is confirmed.</Success>}
      <ErrorBanner message={error} />
      <p className="mt-6 text-center text-sm">
        <Link to={user ? "/" : "/login"} className="font-medium text-brand-700 hover:underline">
          {user ? "Go to DocuMind" : "Log in"}
        </Link>
      </p>
    </AuthShell>
  );
}

export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setSent((await api.requestPasswordReset(email)).message);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title="Reset your password" subtitle="We will email you a link to choose a new password.">
      {sent ? <Success>{sent}</Success> : (
        <form onSubmit={submit} className="space-y-4">
          <ErrorBanner message={error} />
          <Field label="Email">
            <input className={inputClass} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Button type="submit" className="w-full" disabled={busy}>{busy ? "Sending…" : "Send reset link"}</Button>
        </form>
      )}
      <p className="mt-6 text-center text-sm">
        <Link to="/login" className="font-medium text-brand-700 hover:underline">Back to log in</Link>
      </p>
    </AuthShell>
  );
}

export function ResetPasswordPage() {
  const { user, logout } = useAuth();
  const [token] = useState(() => takeTokenFromUrl("/reset-password"));
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(token ? null : INCOMPLETE_LINK);
  const [busy, setBusy] = useState(false);

  const weak = weakPassword(password);
  const mismatch = repeat.length > 0 && repeat !== password;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.confirmPasswordReset(token, password);
      setDone(true);
      if (user) logout(); // the server has ended every session of this account
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title="Choose a new password" subtitle="Every device that is logged in will be logged out.">
      {done ? <Success>Your password has been changed. You can now log in with the new password.</Success> : (
        <form onSubmit={submit} className="space-y-4">
          <ErrorBanner message={error} />
          <Field label="New password">
            <input
              className={inputClass} type="password" autoComplete="new-password" required minLength={8} maxLength={128}
              value={password} onChange={(e) => setPassword(e.target.value)} aria-describedby="new-pw-hint"
            />
            <span id="new-pw-hint" className={`mt-1 block text-xs ${weak ? "text-red-700" : "text-ink-soft"}`}>
              At least 8 characters, with a letter and a digit.
            </span>
          </Field>
          <Field label="Repeat new password">
            <input className={inputClass} type="password" autoComplete="new-password" required value={repeat} onChange={(e) => setRepeat(e.target.value)} />
            {mismatch && <span className="mt-1 block text-xs text-red-700">The passwords do not match.</span>}
          </Field>
          <Button type="submit" className="w-full" disabled={busy || weak || mismatch || !token}>
            {busy ? "Saving…" : "Set new password"}
          </Button>
        </form>
      )}
      <p className="mt-6 text-center text-sm">
        <Link to="/login" className="font-medium text-brand-700 hover:underline">Log in</Link>
      </p>
    </AuthShell>
  );
}
