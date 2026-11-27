import type { Citation } from "../api/types";
import { pageLabel } from "./ui";

/** Source cards under an answer: document name, exact page(s), excerpt, and a
 *  button that opens the PDF at that page. `active` = the card the user just
 *  picked via an inline [n] marker. */
export function Citations({
  citations, active, onSelect, onOpen,
}: {
  citations: Citation[];
  active: number | null;
  onSelect: (n: number | null) => void;
  onOpen: (c: Citation) => void;
}) {
  if (!citations.length) return null;
  return (
    <div className="mt-3 space-y-2" aria-label="Sources">
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Sources</p>
      {citations.map((c) => {
        const expanded = active === c.number;
        return (
          <div key={`${c.number}-${c.chunk_id}`} id={`cite-${c.chunk_id}`} className={`rounded-lg border border-l-4 border-l-brand-500 bg-brand-50/40 ${expanded ? "border-brand-300" : "border-slate-200"}`}>
            <div className="flex items-center gap-2 px-3 py-2 text-sm">
              <button
                onClick={() => onSelect(expanded ? null : c.number)}
                className="flex min-w-0 flex-1 items-center gap-2 text-left"
                aria-expanded={expanded}
              >
                <span className="flex h-5 min-w-5 items-center justify-center rounded border border-brand-300 bg-white px-1 text-xs font-semibold text-brand-800">
                  {c.number}
                </span>
                <span className="truncate font-medium text-ink">{c.filename}</span>
                <span className="shrink-0 text-ink-muted">· {pageLabel(c.page_start, c.page_end)}</span>
              </button>
              <button onClick={() => onOpen(c)} className="shrink-0 rounded-md border border-brand-300 bg-white px-2 py-0.5 text-xs font-medium text-brand-800 hover:bg-brand-50">
                View page
              </button>
            </div>
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
