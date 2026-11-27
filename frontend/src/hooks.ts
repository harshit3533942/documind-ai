import { useCallback, useEffect, useRef, useState } from "react";
import { api, watchDocumentEvents, type DocumentEvent } from "./api/client";
import { ACTIVE_STATUSES, type DocumentItem } from "./api/types";

/** "page x of y" while a document is being processed. */
export interface Progress {
  done: number;
  total: number;
}

// Polling is only the safety net: live updates come from the event stream.
const POLL_WHEN_LIVE_MS = 15_000;
const POLL_WITHOUT_LIVE_MS = 2_000;

/**
 * Live status stream of the user's documents (GET /documents/events, SSE).
 * Returns whether updates are currently live; if not, callers should poll.
 */
export function useDocumentEvents(onEvent: (e: DocumentEvent) => void): boolean {
  const [live, setLive] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent; // always the latest callback, without reconnecting
  useEffect(() => {
    const ctrl = new AbortController();
    void watchDocumentEvents((e) => handler.current(e), setLive, ctrl.signal);
    return () => ctrl.abort();
  }, []);
  return live;
}

/**
 * The user's documents, kept up to date by the live event stream. Each status
 * change is applied immediately; finished documents are re-fetched for their
 * final details (pages, error). A slow poll runs while anything is unfinished
 * in case an event was missed (pub/sub is fire-and-forget).
 */
export function useDocuments() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [progress, setProgress] = useState<Record<string, Progress>>({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const known = useRef<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const page = await api.listDocuments({ limit: 100 });
      known.current = new Set(page.items.map((d) => d.id));
      setDocuments(page.items);
      setTotal(page.total);
      setError(null);
      return page.items;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load documents");
      return [];
    } finally {
      setLoading(false);
    }
  }, []);

  const live = useDocumentEvents((e) => {
    if (!known.current.has(e.document_id)) {
      void refresh(); // e.g. uploaded in another tab
      return;
    }
    setDocuments((prev) => prev.map((d) =>
      d.id === e.document_id ? { ...d, status: e.status, error_message: e.error_message ?? (e.status === "failed" ? d.error_message : null) } : d));
    setProgress((prev) => {
      const next = { ...prev };
      if (e.status === "processing" && e.pages_total) next[e.document_id] = { done: e.pages_done ?? 0, total: e.pages_total };
      else delete next[e.document_id];
      return next;
    });
    if (e.status === "completed" || e.status === "failed") void refresh();
  });

  // Fallback poll while something is unfinished; its interval follows `live`.
  const liveRef = useRef(live);
  liveRef.current = live;
  const schedule = useCallback(() => {
    window.clearTimeout(timer.current);
    const tick = async () => {
      const items = await refresh();
      if (items.some((d) => ACTIVE_STATUSES.includes(d.status))) {
        timer.current = window.setTimeout(tick, liveRef.current ? POLL_WHEN_LIVE_MS : POLL_WITHOUT_LIVE_MS);
      }
    };
    void tick();
  }, [refresh]);

  useEffect(() => {
    schedule();
    return () => window.clearTimeout(timer.current);
  }, [schedule]);

  /** Call after an upload/reprocess so the fallback poll starts again. */
  const restartPolling = schedule;

  return { documents, progress, total, loading, error, live, refresh, restartPolling };
}
