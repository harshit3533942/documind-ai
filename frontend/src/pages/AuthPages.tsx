import { useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button, Card, ErrorBanner, inputClass } from "../components/ui";

export function AuthShell({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-b from-brand-50 to-page px-4 py-12">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2 text-xl font-semibold text-ink">
          <img src="/favicon.svg" alt="" className="h-9 w-9" />
          DocuMind <span className="text-brand-600">AI</span>
        </div>
        <Card className="p-6 sm:p-8">
          <h1 className="text-xl font-semibold text-ink">{title}</h1>
          <p className="mt-1 text-sm text-ink-muted">{subtitle}</p>
          <div className="mt-6">{children}</div>
        </Card>
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink">{label}</span>
      {children}
    </label>
  );
}

export const errorMessage = (err: unknown) => (err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
const message = errorMessage;

export function LoginPage() {
  const { user, login } = useAuth();
  const from = (useLocation().state as { from?: string } | null)?.from ?? "/";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={from} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title="Welcome back" subtitle="Log in to ask questions about your documents.">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner message={error} />
        <Field label="Email">
          <input className={inputClass} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password">
          <input className={inputClass} type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <p className="text-right text-sm">
          <Link to="/forgot-password" className="text-brand-700 underline-offset-2 hover:underline">Forgot password?</Link>
        </p>
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Logging in…" : "Log in"}</Button>
      </form>
      <p className="mt-6 text-center text-sm text-ink-muted">
        New here? <Link to="/register" className="font-medium text-brand-700 underline-offset-2 hover:underline">Create an account</Link>
      </p>
    </AuthShell>
  );
}

export function RegisterPage() {
  const { user, register } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [justRegistered, setJustRegistered] = useState(false);

  // One place decides where a logged-in user goes. (A separate navigate()
  // after register() raced with this re-render - React Router 7 let the
  // re-render win and new users landed on "/" instead of the upload page.)
  if (user) return <Navigate to={justRegistered ? "/upload" : "/"} replace />;

  // Mirrors the backend rule (the backend remains the authority).
  const weak = password.length > 0 && (password.length < 8 || !/[a-zA-Z]/.test(password) || !/\d/.test(password));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setJustRegistered(true);
    try {
      await register(name, email, password); // sets `user` -> the guard above redirects
    } catch (err) {
      setJustRegistered(false);
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title="Create your account" subtitle="Upload documents and get answers with page citations.">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner message={error} />
        <Field label="Name">
          <input className={inputClass} autoComplete="name" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Email">
          <input className={inputClass} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password">
          <input className={inputClass} type="password" autoComplete="new-password" required minLength={8} maxLength={128}
            value={password} onChange={(e) => setPassword(e.target.value)} aria-describedby="pw-hint" />
          <span id="pw-hint" className={`mt-1 block text-xs ${weak ? "text-red-700" : "text-ink-soft"}`}>
            At least 8 characters, with a letter and a digit.
          </span>
        </Field>
        <Button type="submit" className="w-full" disabled={busy || weak}>{busy ? "Creating account…" : "Create account"}</Button>
      </form>
      <p className="mt-6 text-center text-sm text-ink-muted">
        Already registered? <Link to="/login" className="font-medium text-brand-700 underline-offset-2 hover:underline">Log in</Link>
      </p>
    </AuthShell>
  );
}
