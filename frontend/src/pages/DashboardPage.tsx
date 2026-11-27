import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Conversation, DocumentStatus } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Card, EmptyState, PageHeader, Spinner, StatusBadge, timeAgo } from "../components/ui";
import { useDocuments } from "../hooks";

const STATUSES: DocumentStatus[] = ["completed", "processing", "queued", "uploaded", "failed"];

export function DashboardPage() {
  const { user } = useAuth();
  const { documents, loading } = useDocuments();
  const [conversations, setConversations] = useState<Conversation[] | null>(null);

  useEffect(() => {
    api.listConversations(5).then((p) => setConversations(p.items)).catch(() => setConversations([]));
  }, []);

  const counts = STATUSES.map((s) => ({ status: s, n: documents.filter((d) => d.status === s).length }));
  const ready = documents.filter((d) => d.status === "completed").length;

  return (
    <>
      <PageHeader title={`Hello, ${user?.name ?? ""}`} subtitle="Your document intelligence workspace." />

      <div className="grid gap-4 sm:grid-cols-3">
        <Card className="border-t-4 border-t-brand-500 p-5">
          <p className="text-sm text-ink-muted">Documents</p>
          <p className="mt-1 text-3xl font-semibold text-ink">{loading ? "–" : documents.length}</p>
          <p className="mt-1 text-xs text-ink-soft">{ready} ready for questions</p>
        </Card>
        <Card className="border-t-4 border-t-emerald-500 p-5">
          <p className="text-sm text-ink-muted">Conversations</p>
          <p className="mt-1 text-3xl font-semibold text-ink">{conversations === null ? "–" : conversations.length}</p>
          <p className="mt-1 text-xs text-ink-soft">most recent shown below</p>
        </Card>
        <Card className="flex flex-col justify-between gap-3 border-t-4 border-t-amber-400 p-5">
          <p className="text-sm text-ink-muted">Quick actions</p>
          <div className="flex flex-wrap gap-2">
            <Link to="/chat" className="rounded-lg border border-brand-600 bg-brand-50 px-3 py-1.5 text-sm font-medium text-brand-800 hover:bg-brand-100">Ask a question</Link>
            <Link to="/upload" className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-ink hover:bg-slate-50">Upload</Link>
          </div>
        </Card>
      </div>

      <div className="mt-8 grid gap-6 lg:grid-cols-2">
        <section className="min-w-0">
          <h2 className="mb-3 font-semibold text-ink">Processing status</h2>
          <Card className="p-5">
            {loading ? <Spinner label="Loading…" /> : (
              <ul className="space-y-2">
                {counts.map(({ status, n }) => (
                  <li key={status} className="flex items-center justify-between text-sm">
                    <StatusBadge status={status} />
                    <span className="font-medium text-ink">{n}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </section>

        <section className="min-w-0">
          <h2 className="mb-3 font-semibold text-ink">Recent conversations</h2>
          {conversations === null ? <Spinner label="Loading…" /> : conversations.length === 0 ? (
            <EmptyState title="No conversations yet">Ask your first question on the <Link className="text-brand-700 underline" to="/chat">Ask AI</Link> page.</EmptyState>
          ) : (
            <Card className="divide-y divide-slate-100">
              {conversations.map((c) => (
                <Link key={c.id} to={`/chat/${c.id}`} className="block px-5 py-3 hover:bg-brand-50/50">
                  <p className="truncate text-sm font-medium text-ink">{c.title}</p>
                  <p className="mt-0.5 text-xs text-ink-soft">{c.message_count} messages · {timeAgo(c.updated_at)}</p>
                </Link>
              ))}
            </Card>
          )}
        </section>
      </div>
    </>
  );
}
