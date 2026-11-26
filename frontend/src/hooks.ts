import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import { ACTIVE_STATUSES, type DocumentItem } from "./api/types";

/**
 * The user's documents, re-fetched every `intervalMs` while any document is
 * still uploaded/queued/processing (status polling). Polling stops by itself
 * once everything is completed or failed.
 */
export function useDocuments(intervalMs = 2000) {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const refresh = useCallback(async () => {
    try {
      const page = await api.listDocuments({ limit: 100 });
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

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      const items = await refresh();
      if (!cancelled && items.some((d) => ACTIVE_STATUSES.includes(d.status))) {
        timer.current = window.setTimeout(tick, intervalMs);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer.current);
    };
  }, [refresh, intervalMs]);

  /** Call after an upload/reprocess so polling starts again. */
  const restartPolling = useCallback(() => {
    window.clearTimeout(timer.current);
    const tick = async () => {
      const items = await refresh();
      if (items.some((d) => ACTIVE_STATUSES.includes(d.status))) timer.current = window.setTimeout(tick, intervalMs);
    };
    void tick();
  }, [refresh, intervalMs]);

  return { documents, total, loading, error, refresh, restartPolling };
}
