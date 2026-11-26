// Small shared UI primitives. Light UI: white/tinted surfaces, dark text,
// brand colour as borders/tints (no white-on-dark controls).
import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { DocumentStatus } from "../api/types";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-brand-50 text-brand-800 border-brand-600 hover:bg-brand-100",
  secondary: "bg-white text-ink border-slate-300 hover:bg-slate-50",
  danger: "bg-red-50 text-red-800 border-red-300 hover:bg-red-100",
  ghost: "bg-transparent text-ink-muted border-transparent hover:bg-slate-100",
};

export function Button({
  variant = "primary", className = "", children, ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-2 rounded-lg border px-3.5 py-2 text-sm font-medium
        transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300
        disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}>{children}</div>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-ink-muted" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-brand-200 border-t-brand-600" />
      {label}
    </span>
  );
}

export function ErrorBanner({ message, onClose }: { message: string | null; onClose?: () => void }) {
  if (!message) return null;
  return (
    <div role="alert" className="flex items-start justify-between gap-3 rounded-lg border border-red-200 border-l-4 border-l-red-500 bg-red-50 px-4 py-3 text-sm text-red-800">
      <span>{message}</span>
      {onClose && <button onClick={onClose} className="text-red-700 hover:text-red-900" aria-label="Dismiss">✕</button>}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
      <p className="font-medium text-ink">{title}</p>
      {children && <div className="mt-2 text-sm text-ink-muted">{children}</div>}
    </div>
  );
}

const STATUS_STYLE: Record<DocumentStatus, string> = {
  uploaded: "bg-slate-50 text-slate-700 border-slate-300",
  queued: "bg-amber-50 text-amber-800 border-amber-300",
  processing: "bg-brand-50 text-brand-800 border-brand-300",
  completed: "bg-emerald-50 text-emerald-800 border-emerald-300",
  failed: "bg-red-50 text-red-800 border-red-300",
};

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const active = status === "queued" || status === "processing";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize ${STATUS_STYLE[status]}`}>
      {active && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {status}
    </span>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-ink-muted">{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}

export const inputClass =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-soft " +
  "focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export function pageLabel(start: number, end: number): string {
  return start === end ? `p. ${start}` : `pp. ${start}–${end}`;
}
