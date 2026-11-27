import { useRef, useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, uploadDocument } from "../api/client";
import { ACTIVE_STATUSES, type DocumentItem } from "../api/types";
import { Card, ErrorBanner, PageHeader, ProcessingProgress, StatusBadge, formatBytes } from "../components/ui";
import { useDocumentEvents, type Progress } from "../hooks";

const MAX_MB = 20; // matches the backend default MAX_UPLOAD_SIZE_MB (backend enforces it)

interface UploadItem {
  key: string;
  file: File;
  progress: number;
  doc?: DocumentItem;
  processing?: Progress; // live "page x of y"
  error?: string;
}

export function UploadPage() {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const update = (key: string, patch: Partial<UploadItem>) =>
    setItems((prev) => prev.map((i) => (i.key === key ? { ...i, ...patch } : i)));

  // Live status: apply each event to the matching upload; fetch final details when done.
  const live = useDocumentEvents((e) => {
    setItems((prev) => prev.map((i) => {
      if (i.doc?.id !== e.document_id) return i;
      const progress = e.status === "processing" && e.pages_total ? { done: e.pages_done ?? 0, total: e.pages_total } : undefined;
      return { ...i, doc: { ...i.doc, status: e.status, error_message: e.error_message ?? i.doc.error_message }, processing: progress };
    }));
    if (e.status === "completed" || e.status === "failed") {
      api.getDocument(e.document_id)
        .then((fresh) => setItems((prev) => prev.map((i) => (i.doc?.id === fresh.id ? { ...i, doc: fresh } : i))))
        .catch(() => {});
    }
  });
  const liveRef = useRef(live);
  liveRef.current = live;

  // Fallback poll until completed/failed (slow while live events arrive).
  const watch = (key: string, doc: DocumentItem) => {
    const poll = async () => {
      try {
        const fresh = await api.getDocument(doc.id);
        setItems((prev) => prev.map((i) => (i.key === key && i.doc && ACTIVE_STATUSES.includes(i.doc.status) ? { ...i, doc: fresh } : i)));
        if (ACTIVE_STATUSES.includes(fresh.status)) window.setTimeout(poll, liveRef.current ? 15_000 : 1500);
      } catch {
        /* stop polling on error; the library page still shows the status */
      }
    };
    window.setTimeout(poll, liveRef.current ? 15_000 : 800);
  };

  const start = (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const key = `${file.name}-${file.size}-${Date.now()}-${Math.random()}`;
      // Quick client-side checks for instant feedback; the server re-checks everything.
      let error: string | undefined;
      if (!file.name.toLowerCase().endsWith(".pdf")) error = "Only PDF files are allowed.";
      else if (file.size > MAX_MB * 1024 * 1024) error = `File is larger than ${MAX_MB} MB.`;
      else if (file.size === 0) error = "The file is empty.";
      setItems((prev) => [{ key, file, progress: 0, error }, ...prev]);
      if (error) continue;
      uploadDocument(file, (p) => update(key, { progress: p }))
        .then((doc) => {
          update(key, { doc, progress: 1 });
          watch(key, doc);
        })
        .catch((err) => update(key, { error: err instanceof ApiError ? err.message : "Upload failed" }));
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) start(e.dataTransfer.files);
  };

  return (
    <>
      <PageHeader title="Upload PDFs" subtitle={`PDF only, up to ${MAX_MB} MB each. Text is extracted, chunked and embedded in the background.`} />

      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`flex flex-col items-center justify-center rounded-2xl border-2 border-dashed px-6 py-14 text-center transition ${
          dragging ? "border-brand-500 bg-brand-50" : "border-slate-300 bg-white"
        }`}
      >
        <svg className="h-10 w-10 text-brand-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
        </svg>
        <p className="mt-3 font-medium text-ink">Drag & drop PDFs here</p>
        <p className="mt-1 text-sm text-ink-muted">or</p>
        <button
          onClick={() => inputRef.current?.click()}
          className="mt-2 rounded-lg border border-brand-600 bg-brand-50 px-4 py-2 text-sm font-medium text-brand-800 hover:bg-brand-100"
        >
          Choose files
        </button>
        <input
          ref={inputRef} type="file" accept="application/pdf,.pdf" multiple className="hidden" data-testid="file-input"
          onChange={(e) => { if (e.target.files) start(e.target.files); e.target.value = ""; }}
        />
      </div>

      {items.length > 0 && (
        <Card className="mt-6 divide-y divide-slate-100">
          {items.map((item) => (
            <div key={item.key} className="px-5 py-4">
              <div className="flex flex-wrap items-center gap-3">
                <p className="min-w-0 flex-1 truncate font-medium text-ink">{item.file.name}</p>
                <span className="text-xs text-ink-soft">{formatBytes(item.file.size)}</span>
                {item.doc && <StatusBadge status={item.doc.status} />}
              </div>
              {item.doc?.status === "processing" && item.processing && <ProcessingProgress {...item.processing} />}
              {!item.error && !item.doc && (
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-valuenow={Math.round(item.progress * 100)}>
                  <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${Math.round(item.progress * 100)}%` }} />
                </div>
              )}
              {item.error && <div className="mt-2"><ErrorBanner message={item.error} /></div>}
              {item.doc?.status === "failed" && <div className="mt-2"><ErrorBanner message={item.doc.error_message ?? "Processing failed"} /></div>}
              {item.doc?.status === "completed" && (
                <p className="mt-2 text-sm text-emerald-800">
                  Ready: {item.doc.total_pages} pages processed.{" "}
                  <Link to={`/chat?doc=${item.doc.id}`} className="font-medium text-brand-700 underline">Ask about this document →</Link>
                </p>
              )}
            </div>
          ))}
        </Card>
      )}
    </>
  );
}
