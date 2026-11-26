import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Conversation } from "../api/types";
import { Button, Card, EmptyState, ErrorBanner, PageHeader, Spinner, timeAgo } from "../components/ui";

export function ConversationsPage() {
  const [items, setItems] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.listConversations(100).then((p) => setItems(p.items)).catch(() => setError("Could not load conversations."));
  useEffect(() => { void load(); }, []);

  const remove = async (c: Conversation) => {
    if (!window.confirm(`Delete the conversation "${c.title}"?`)) return;
    try {
      await api.deleteConversation(c.id);
      setItems((prev) => prev?.filter((x) => x.id !== c.id) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  };

  return (
    <>
      <PageHeader
        title="Conversation history"
        subtitle="Open a conversation to continue it - follow-up questions use the earlier messages."
        actions={<Link to="/chat" className="rounded-lg border border-brand-600 bg-brand-50 px-3.5 py-2 text-sm font-medium text-brand-800 hover:bg-brand-100">New conversation</Link>}
      />
      <ErrorBanner message={error} onClose={() => setError(null)} />
      {items === null ? <Spinner label="Loading…" /> : items.length === 0 ? (
        <EmptyState title="No conversations yet"><Link to="/chat" className="text-brand-700 underline">Ask your first question</Link>.</EmptyState>
      ) : (
        <Card className="mt-4 divide-y divide-slate-100">
          {items.map((c) => (
            <div key={c.id} className="flex flex-wrap items-center gap-3 px-5 py-4 hover:bg-slate-50/60">
              <Link to={`/chat/${c.id}`} className="min-w-0 flex-1">
                <p className="truncate font-medium text-ink">{c.title}</p>
                {c.last_message_preview && <p className="mt-0.5 truncate text-sm text-ink-muted">{c.last_message_preview}</p>}
                <p className="mt-1 text-xs text-ink-soft">{c.message_count} messages · updated {timeAgo(c.updated_at)}</p>
              </Link>
              <div className="flex gap-2">
                <Link to={`/chat/${c.id}`} className="rounded-lg border border-brand-300 bg-white px-3 py-1.5 text-sm font-medium text-brand-800 hover:bg-brand-50">Continue</Link>
                <Button variant="danger" className="py-1.5" onClick={() => remove(c)}>Delete</Button>
              </div>
            </div>
          ))}
        </Card>
      )}
    </>
  );
}
