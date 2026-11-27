import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { DocumentItem } from "../api/types";
import { Button, Card, EmptyState, ErrorBanner, PageHeader, ProcessingProgress, Spinner, StatusBadge, formatBytes, timeAgo } from "../components/ui";
import { useDocuments } from "../hooks";

export function DocumentsPage() {
  const { documents, progress, live, loading, error, refresh, restartPolling } = useDocuments();
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const remove = async (doc: DocumentItem) => {
    if (!window.confirm(`Delete "${doc.filename}"? Its text and embeddings are removed too. This cannot be undone.`)) return;
    setBusyId(doc.id);
    try {
      await api.deleteDocument(doc.id);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusyId(null);
    }
  };

  const reprocess = async (doc: DocumentItem) => {
    setBusyId(doc.id);
    try {
      await api.reprocessDocument(doc.id);
      restartPolling();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not reprocess");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Document library"
        subtitle={live ? "Live: status updates appear instantly while documents are processed." : "Status updates automatically while documents are being processed."}
        actions={<Link to="/upload" className="rounded-lg border border-brand-600 bg-brand-50 px-3.5 py-2 text-sm font-medium text-brand-800 hover:bg-brand-100">Upload PDF</Link>}
      />
      <div className="space-y-3">
        <ErrorBanner message={error} />
        <ErrorBanner message={actionError} onClose={() => setActionError(null)} />
      </div>

      {loading ? <Spinner label="Loading documents…" /> : documents.length === 0 ? (
        <EmptyState title="No documents yet"><Link to="/upload" className="text-brand-700 underline">Upload your first PDF</Link> to get started.</EmptyState>
      ) : (
        <Card className="mt-4 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-ink-soft">
                <tr>
                  <th className="px-5 py-3 font-semibold">Document</th>
                  <th className="px-3 py-3 font-semibold">Status</th>
                  <th className="px-3 py-3 font-semibold">Pages</th>
                  <th className="px-3 py-3 font-semibold">Size</th>
                  <th className="px-3 py-3 font-semibold">Uploaded</th>
                  <th className="px-5 py-3 text-right font-semibold">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {documents.map((d) => (
                  <tr key={d.id} className="align-top hover:bg-slate-50/60">
                    <td className="px-5 py-3">
                      <p className="max-w-xs truncate font-medium text-ink" title={d.filename}>{d.filename}</p>
                      {d.error_message && <p className="mt-1 max-w-md text-xs text-red-700">{d.error_message}</p>}
                    </td>
                    <td className="px-3 py-3">
                      <StatusBadge status={d.status} />
                      {d.status === "processing" && progress[d.id] && <ProcessingProgress {...progress[d.id]!} />}
                    </td>
                    <td className="px-3 py-3 text-ink-muted">{d.total_pages ?? "–"}</td>
                    <td className="px-3 py-3 text-ink-muted">{formatBytes(d.file_size)}</td>
                    <td className="px-3 py-3 text-ink-muted">{timeAgo(d.created_at)}</td>
                    <td className="px-5 py-3">
                      <div className="flex justify-end gap-2">
                        {d.status === "completed" && (
                          <Link to={`/chat?doc=${d.id}`} className="rounded-lg border border-brand-300 bg-white px-2.5 py-1 text-xs font-medium text-brand-800 hover:bg-brand-50">Ask</Link>
                        )}
                        {(d.status === "failed" || d.status === "completed") && (
                          <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busyId === d.id} onClick={() => reprocess(d)}>
                            {d.status === "failed" ? "Retry" : "Reprocess"}
                          </Button>
                        )}
                        <Button variant="danger" className="px-2.5 py-1 text-xs" disabled={busyId === d.id} onClick={() => remove(d)}>Delete</Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
