import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, downloadConversation } from "../api/client";
import type { Conversation } from "../api/types";
import { Button, Card, EmptyState, ErrorBanner, PageHeader, Spinner, inputClass, timeAgo } from "../components/ui";

const SEARCH_DELAY_MS = 300; // debounce: one request after the user pauses typing, not one per key

export function ConversationsPage() {
  const [items, setItems] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);
  const latest = useRef(0); // ignore responses of older searches that arrive late

  const load = (q: string) => {
    const ticket = ++latest.current;
    api.listConversations(100, 0, q)
      .then((p) => { if (ticket === latest.current) setItems(p.items); })
      .catch(() => setError("Could not load conversations."));
  };

  useEffect(() => {
    const t = window.setTimeout(() => load(query), query ? SEARCH_DELAY_MS : 0);
    return () => window.clearTimeout(t);
  }, [query]);

  const remove = async (c: Conversation) => {
    if (!window.confirm(`Delete the conversation "${c.title}"?`)) return;
    try {
      await api.deleteConversation(c.id);
      setItems((prev) => prev?.filter((x) => x.id !== c.id) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  };

  const saveTitle = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!editing) return;
    const { id, title } = editing;
    setEditing(null);
    const current = items?.find((c) => c.id === id);
    if (!title.trim() || title.trim() === current?.title) return;
    try {
      const updated = await api.renameConversation(id, title);
      setItems((prev) => prev?.map((c) => (c.id === id ? { ...c, title: updated.title } : c)) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rename failed");
    }
  };

  const exportAs = async (c: Conversation, format: "markdown" | "json") => {
    try {
      await downloadConversation(c.id, format);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    }
  };

  return (
    <>
      <PageHeader
        title="Conversation history"
        subtitle="Open a conversation to continue it - follow-up questions use the earlier messages."
        actions={<Link to="/chat" className="rounded-lg border border-brand-600 bg-brand-50 px-3.5 py-2 text-sm font-medium text-brand-800 hover:bg-brand-100">New conversation</Link>}
      />
      <div className="mb-4">
        <label htmlFor="conversation-search" className="sr-only">Search conversations</label>
        <input
          id="conversation-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder="Search titles, questions and answers…" className={`${inputClass} max-w-md`} maxLength={200}
        />
      </div>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      {items === null ? <Spinner label="Loading…" /> : items.length === 0 ? (
        query.trim()
          ? <EmptyState title={`No conversations match "${query.trim()}"`} />
          : <EmptyState title="No conversations yet"><Link to="/chat" className="text-brand-700 underline">Ask your first question</Link>.</EmptyState>
      ) : (
        <Card className="mt-4 divide-y divide-slate-100">
          {items.map((c) => (
            <div key={c.id} className="flex flex-wrap items-center gap-3 px-5 py-4 hover:bg-slate-50/60">
              {editing?.id === c.id ? (
                <form onSubmit={saveTitle} className="min-w-0 flex-1">
                  <label htmlFor={`title-${c.id}`} className="sr-only">Conversation title</label>
                  <input
                    id={`title-${c.id}`} autoFocus value={editing.title} maxLength={200} className={inputClass}
                    onChange={(e) => setEditing({ id: c.id, title: e.target.value })}
                    onBlur={() => void saveTitle()}
                    onKeyDown={(e) => e.key === "Escape" && setEditing(null)}
                  />
                </form>
              ) : (
                <Link to={`/chat/${c.id}`} className="min-w-0 flex-1">
                  <p className="truncate font-medium text-ink">{c.title}</p>
                  {c.match_snippet
                    ? <p className="mt-0.5 truncate text-sm text-ink-muted" title={c.match_snippet}>Match: {c.match_snippet}</p>
                    : c.last_message_preview && <p className="mt-0.5 truncate text-sm text-ink-muted">{c.last_message_preview}</p>}
                  <p className="mt-1 text-xs text-ink-soft">{c.message_count} messages · updated {timeAgo(c.updated_at)}</p>
                </Link>
              )}
              <div className="flex flex-wrap gap-2">
                <Link to={`/chat/${c.id}`} className="rounded-lg border border-brand-300 bg-white px-3 py-1.5 text-sm font-medium text-brand-800 hover:bg-brand-50">Continue</Link>
                <Button variant="secondary" className="py-1.5" onClick={() => setEditing({ id: c.id, title: c.title })}>Rename</Button>
                <Button variant="secondary" className="py-1.5" onClick={() => exportAs(c, "markdown")} title="Download as Markdown">Export .md</Button>
                <Button variant="secondary" className="py-1.5" onClick={() => exportAs(c, "json")} title="Download as JSON">.json</Button>
                <Button variant="danger" className="py-1.5" onClick={() => remove(c)}>Delete</Button>
              </div>
            </div>
          ))}
        </Card>
      )}
    </>
  );
}
