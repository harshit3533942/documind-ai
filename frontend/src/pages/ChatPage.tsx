import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError, api, askStream } from "../api/client";
import type { Conversation, DocumentItem, Message } from "../api/types";
import { Markdown } from "../components/Markdown";
import { Button, Card, ErrorBanner, Spinner, timeAgo } from "../components/ui";

const SUGGESTIONS = ["Summarise the key points", "What are the deadlines mentioned?", "What does it say about costs?"];

function MessageBubble({ m }: { m: Message }) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm border border-brand-200 bg-brand-50 px-4 py-2.5 text-sm text-ink">{m.content}</div>
      </div>
    );
  }
  // Answers only: source boxes are not shown, so the [n] markers pointing at them are dropped too.
  // (Citations are still stored by the backend and returned by the API.)
  return (
    <div className="flex justify-start">
      <div className={`max-w-[92%] rounded-2xl rounded-bl-sm border bg-white px-4 py-3 shadow-sm ${m.found === false ? "border-amber-300" : "border-slate-200"}`}>
        <div className="break-words text-sm leading-relaxed text-ink"><Markdown text={stripCitationMarkers(m)} /></div>
        {m.found === false && (
          <p className="mt-2 text-xs text-amber-800">Tip: check that the right documents are uploaded and finished processing, or rephrase the question.</p>
        )}
      </div>
    </div>
  );
}

/** Remove citation markers like [1] or [2][3] - only numbers that really are citations of this
 *  message, and never inside `code`, so things like `scores[1]` stay intact. */
function stripCitationMarkers(m: Message): string {
  // While streaming, the citations are not known yet: hide every [n] marker
  // (the final saved message replaces the draft and is filtered precisely).
  const streaming = m.id.startsWith("streaming-");
  const numbers = new Set(m.citations.map((c) => c.number));
  if (numbers.size === 0 && !streaming) return m.content;
  const parts = m.content.split(/(```[\s\S]*?```|`[^`\n]*`)/); // odd indexes = code
  return parts
    .map((part, i) =>
      i % 2
        ? part
        : part
            .replace(/[ \t]*((?:\[\d+\])+)/g, (whole, group: string) =>
              streaming || [...group.matchAll(/\d+/g)].every((n) => numbers.has(Number(n[0]))) ? "" : whole)
            .replace(/[ \t]+([.,;:!?])/g, "$1"),
    )
    .join("")
    .trim();
}

export function ChatPage() {
  const { conversationId } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [selected, setSelected] = useState<string[]>(() => (searchParams.get("doc") ? [searchParams.get("doc")!] : []));
  const [question, setQuestion] = useState("");
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [sending, setSending] = useState(false);
  const [streaming, setStreaming] = useState(false); // first token has arrived
  const [error, setError] = useState<string | null>(null);
  const justCreated = useRef<string | null>(null);
  const streamAbort = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const loadConversations = () => api.listConversations(30).then((p) => setConversations(p.items)).catch(() => {});

  useEffect(() => {
    void loadConversations();
    api.listDocuments({ limit: 100, status: "completed" }).then((p) => setDocuments(p.items)).catch(() => {});
  }, []);

  // Load history when opening an existing conversation (skip the one we just created locally).
  useEffect(() => {
    if (!conversationId) {
      setMessages([]);
      return;
    }
    if (justCreated.current === conversationId) return;
    setLoadingHistory(true);
    setError(null);
    api.listMessages(conversationId)
      .then((p) => setMessages(p.items))
      .catch((err) => setError(err instanceof ApiError && err.status === 404 ? "Conversation not found." : "Could not load conversation."))
      .finally(() => setLoadingHistory(false));
  }, [conversationId]);

  // Braces matter: newer browsers return a Promise from scrollIntoView, and React would
  // treat any returned value as a cleanup function and crash calling it.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending]);

  // Stop a running stream when leaving the page (otherwise it would keep
  // reading and call setState on an unmounted component).
  useEffect(() => () => streamAbort.current?.abort(), []);

  const send = async (text: string) => {
    const q = text.trim();
    if (!q || sending) return;
    setSending(true);
    setStreaming(false);
    setError(null);
    setQuestion("");
    const stamp = Date.now();
    const now = new Date().toISOString();
    const optimistic: Message = {
      id: `pending-${stamp}`, role: "user", content: q, created_at: now,
      citations: [], found: null, mode: null, grounded: null, document_ids: selected,
    };
    const draftId = `streaming-${stamp}`;
    setMessages((prev) => [...prev, optimistic]);
    const abort = new AbortController();
    streamAbort.current = abort;
    try {
      const res = await askStream(q, conversationId, selected, {
        onMeta: (meta) => {
          // New conversation: put its id in the URL right away. justCreated stops
          // the history effect from reloading (and wiping) the live messages.
          if (!conversationId) {
            justCreated.current = meta.conversation_id;
            navigate(`/chat/${meta.conversation_id}`, { replace: true });
          }
        },
        onToken: (piece) => {
          setStreaming(true);
          setMessages((prev) => {
            const draft = prev.find((m) => m.id === draftId);
            if (!draft) {
              return [...prev, { ...optimistic, id: draftId, role: "assistant", content: piece, document_ids: null }];
            }
            return prev.map((m) => (m.id === draftId ? { ...m, content: m.content + piece } : m));
          });
        },
      }, abort.signal);
      // The saved answer is authoritative (citations, found flag, final text).
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== optimistic.id && m.id !== draftId), res.user_message, res.assistant_message,
      ]);
      void loadConversations();
    } catch (err) {
      if (abort.signal.aborted) return;
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id && m.id !== draftId));
      setQuestion(q); // give the text back so the user can retry
      setError(err instanceof ApiError ? err.message : "Could not get an answer.");
    } finally {
      if (streamAbort.current === abort) streamAbort.current = null;
      if (!abort.signal.aborted) {
        setSending(false);
        setStreaming(false);
      }
    }
  };

  const newConversation = () => {
    streamAbort.current?.abort();
    setSending(false);
    setStreaming(false);
    justCreated.current = null;
    navigate("/chat");
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void send(question);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send(question);
    }
  };
  const toggle = (id: string) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  return (
    <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
      {/* Sidebar: conversations + document selection */}
      <aside className="order-2 min-w-0 space-y-5 lg:order-1">
        <Button variant="primary" className="w-full" onClick={newConversation}>+ New conversation</Button>

        <Card className="p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Search in</p>
          {documents.length === 0 ? (
            <p className="mt-2 text-sm text-ink-muted">No processed documents. <Link to="/upload" className="text-brand-700 underline">Upload one</Link>.</p>
          ) : (
            <>
              <p className="mt-1 text-xs text-ink-soft">{selected.length ? `${selected.length} selected` : "All documents"}</p>
              <ul className="mt-2 max-h-52 space-y-1 overflow-y-auto">
                {documents.map((d) => (
                  <li key={d.id}>
                    <label className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-slate-50">
                      <input type="checkbox" className="accent-brand-600" checked={selected.includes(d.id)} onChange={() => toggle(d.id)} />
                      <span className="truncate text-ink" title={d.filename}>{d.filename}</span>
                    </label>
                  </li>
                ))}
              </ul>
              {selected.length > 0 && <button className="mt-2 text-xs text-brand-700 underline" onClick={() => setSelected([])}>Search all documents</button>}
            </>
          )}
        </Card>

        <Card className="p-2">
          <p className="px-2 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide text-ink-soft">Recent</p>
          <ul className="max-h-80 overflow-y-auto">
            {conversations.map((c) => (
              <li key={c.id}>
                <Link
                  to={`/chat/${c.id}`}
                  onClick={() => (justCreated.current = null)}
                  className={`block rounded-lg px-2 py-2 text-sm ${c.id === conversationId ? "bg-brand-50 text-brand-800 ring-1 ring-brand-200" : "text-ink hover:bg-slate-50"}`}
                >
                  <span className="block truncate font-medium">{c.title}</span>
                  <span className="text-xs text-ink-soft">{timeAgo(c.updated_at)}</span>
                </Link>
              </li>
            ))}
            {conversations.length === 0 && <li className="px-2 py-2 text-sm text-ink-muted">No conversations yet.</li>}
          </ul>
        </Card>
      </aside>

      {/* Conversation */}
      <section className="order-1 flex min-h-[70vh] min-w-0 flex-col lg:order-2">
        <Card className="flex flex-1 flex-col overflow-hidden">
          <div className="flex-1 space-y-4 overflow-y-auto p-4 sm:p-6" aria-live="polite">
            {loadingHistory && <Spinner label="Loading conversation…" />}
            {!loadingHistory && messages.length === 0 && (
              <div className="py-12 text-center">
                <h2 className="text-lg font-semibold text-ink">Ask anything about your documents</h2>
                <p className="mt-1 text-sm text-ink-muted">Answers come only from your PDFs, with page references you can check.</p>
                <div className="mt-5 flex flex-wrap justify-center gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => void send(s)} className="rounded-full border border-brand-200 bg-brand-50 px-3 py-1.5 text-sm text-brand-800 hover:bg-brand-100">{s}</button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m) => <MessageBubble key={m.id} m={m} />)}
            {sending && !streaming && <Spinner label="Searching your documents…" />}
            <div ref={bottomRef} />
          </div>

          <form onSubmit={onSubmit} className="border-t border-slate-200 bg-slate-50/60 p-3 sm:p-4">
            <ErrorBanner message={error} onClose={() => setError(null)} />
            <div className={`flex items-end gap-2 ${error ? "mt-3" : ""}`}>
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={onKeyDown}
                rows={2}
                maxLength={2000}
                placeholder="Ask a question… (Enter to send, Shift+Enter for a new line)"
                aria-label="Your question"
                className="min-h-[44px] flex-1 resize-y rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-soft focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
              />
              <Button type="submit" disabled={sending || !question.trim()}>Send</Button>
            </div>
          </form>
        </Card>
      </section>
    </div>
  );
}
