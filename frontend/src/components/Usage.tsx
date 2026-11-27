import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { QuotaSummary } from "../api/types";
import { Card, formatBytes } from "./ui";

function Meter({ label, used, limit, format = String }: {
  label: string; used: number; limit: number; format?: (n: number) => string;
}) {
  const unlimited = limit === 0;
  const pct = unlimited ? 0 : Math.min(100, Math.round((used / limit) * 100));
  const tone = pct >= 90 ? "bg-red-500" : pct >= 75 ? "bg-amber-500" : "bg-brand-500";
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-ink-muted">{label}</span>
        <span className="font-medium text-ink">{format(used)} {unlimited ? "(unlimited)" : `of ${format(limit)}`}</span>
      </div>
      {!unlimited && (
        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-label={label}
          aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

/** Quota usage (documents, storage, questions today). */
export function UsageCard({ refreshKey = 0 }: { refreshKey?: number }) {
  const [data, setData] = useState<QuotaSummary | null>(null);
  useEffect(() => {
    api.usage().then(setData).catch(() => setData(null));
  }, [refreshKey]);
  if (!data) return null;
  const { usage, limits } = data;
  return (
    <section aria-label="Usage">
    <Card className="space-y-3 p-5">
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Your usage</p>
      <Meter label="Documents" used={usage.documents} limit={limits.max_documents} />
      <Meter label="Storage" used={usage.storage_bytes} limit={limits.max_storage_bytes} format={formatBytes} />
      <Meter label="Questions today" used={usage.questions_today} limit={limits.max_questions_per_day} />
      {usage.trashed_documents > 0 && (
        <p className="text-xs text-ink-soft">
          Includes {usage.trashed_documents} document{usage.trashed_documents === 1 ? "" : "s"} in the trash.
        </p>
      )}
    </Card>
    </section>
  );
}
