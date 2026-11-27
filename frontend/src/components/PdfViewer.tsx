import { useEffect, useState } from "react";
import { ApiError, fetchDocumentFile } from "../api/client";
import { Button, Spinner } from "./ui";

export interface ViewerTarget {
  documentId: string;
  filename: string;
  page: number;
  excerpt?: string;
}

// The file needs the Authorization header, so it cannot be a plain <iframe src>
// URL: it is fetched once, kept as a Blob URL for this browser session and
// reused when another citation of the same document is opened.
const blobUrls = new Map<string, Promise<string>>();

function blobUrlFor(documentId: string): Promise<string> {
  let url = blobUrls.get(documentId);
  if (!url) {
    url = fetchDocumentFile(documentId).then((blob) => URL.createObjectURL(blob));
    url.catch(() => blobUrls.delete(documentId)); // allow a retry after an error
    blobUrls.set(documentId, url);
  }
  return url;
}

/** Modal with the browser's built-in PDF viewer, opened at the cited page. */
export function PdfViewer({ target, onClose }: { target: ViewerTarget; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setUrl(null);
    setError(null);
    blobUrlFor(target.documentId)
      .then((u) => alive && setUrl(u))
      .catch((err) => alive && setError(err instanceof ApiError ? err.message : "Could not open the document."));
    return () => { alive = false; };
  }, [target.documentId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // "#page=N" is understood by the PDF viewers of Chrome, Edge, Firefox and Safari.
  const src = url ? `${url}#page=${target.page}` : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-2 sm:p-6" onClick={onClose} role="dialog" aria-modal="true" aria-label={`${target.filename}, page ${target.page}`}>
      <div className="flex h-full w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-slate-200 px-4 py-2.5">
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-ink" title={target.filename}>{target.filename}</p>
            <p className="text-xs text-ink-muted">Page {target.page}</p>
          </div>
          {src && (
            <a href={src} target="_blank" rel="noopener noreferrer" className="text-sm text-brand-700 underline">Open in new tab</a>
          )}
          <Button variant="secondary" onClick={onClose} aria-label="Close viewer">Close</Button>
        </div>
        {target.excerpt && (
          <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
            <span className="font-semibold">Cited passage: </span>“{target.excerpt}”
          </div>
        )}
        <div className="flex flex-1 items-center justify-center bg-slate-50">
          {error && <p className="px-4 text-sm text-red-700">{error}</p>}
          {!error && !src && <Spinner label="Opening document…" />}
          {/* key: a new page number must reload the frame, changing only the #hash may not jump */}
          {src && <iframe key={src} src={src} title={target.filename} className="h-full w-full border-0" />}
        </div>
      </div>
    </div>
  );
}
