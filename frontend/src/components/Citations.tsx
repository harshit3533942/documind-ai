import { useState } from "react";
import type { Citation } from "../api/types";
import { pageLabel } from "./ui";

/** Source cards under an answer: document name, exact page(s), excerpt. */
export function Citations({ citations }: { citations: Citation[] }) {
  const [open, setOpen] = useState<number | null>(citations.length === 1 ? citations[0]!.number : null);
  if (!citations.length) return null;
  return (
    <div className="mt-3 space-y-2" aria-label="Sources">
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Sources</p>
      {citations.map((c) => {
        const expanded = open === c.number;
        return (
          <div key={`${c.number}-${c.chunk_id}`} className="rounded-lg border border-slate-200 border-l-4 border-l-brand-500 bg-brand-50/40">
            <button
              onClick={() => setOpen(expanded ? null : c.number)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
              aria-expanded={expanded}
            >
              <span className="flex h-5 min-w-5 items-center justify-center rounded border border-brand-300 bg-white px-1 text-xs font-semibold text-brand-800">
                {c.number}
              </span>
              <span className="truncate font-medium text-ink">{c.filename}</span>
              <span className="shrink-0 text-ink-muted">· {pageLabel(c.page_start, c.page_end)}</span>
              <span className="ml-auto shrink-0 text-xs text-ink-soft">relevance {Math.round(c.score * 100)}%</span>
            </button>
            {expanded && (
              <blockquote className="border-t border-slate-200 px-4 py-3 text-sm leading-relaxed text-ink-muted">
                “{c.excerpt}”
                {(c.chunk_page_start !== c.page_start || c.chunk_page_end !== c.page_end) && (
                  <span className="mt-1 block text-xs text-ink-soft">
                    Passage spans {pageLabel(c.chunk_page_start, c.chunk_page_end)}; quoted text is on {pageLabel(c.page_start, c.page_end)}.
                  </span>
                )}
              </blockquote>
            )}
          </div>
        );
      })}
    </div>
  );
}
