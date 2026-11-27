// Typed client for the FastAPI backend. All URLs are relative ("/api/v1/..."):
// the Vite dev server (and nginx in production) proxies them to FastAPI.
import type {
  AskResponse, Conversation, DocumentItem, DocumentStatus, Message, Page, Token, User,
} from "./types";

const BASE = "/api/v1";

// --- session tokens ------------------------------------------------------------
// Access token (15 min JWT): kept ONLY in memory - never in localStorage, so an
// XSS bug cannot read a token that stays valid after the page is closed.
// Refresh token (14 days): an httpOnly cookie the browser sends only to
// /api/v1/auth/*. JavaScript never sees it. After a reload the memory is empty,
// so the app calls /auth/refresh once to get a new access token ("silent login").
let accessToken: string | null = null;
localStorage.removeItem("documind.token"); // tokens stored by older versions of the app

// Not a secret, just a hint "this browser probably has a session": JavaScript
// cannot see the httpOnly cookie, and calling /auth/refresh without one is a 401
// (a red console error) on every visit to the login page.
const SESSION_HINT = "documind.session";

export const tokenStore = {
  get: (): string | null => accessToken,
  set: (token: string) => {
    accessToken = token;
    localStorage.setItem(SESSION_HINT, "1");
  },
  clear: () => {
    accessToken = null;
    localStorage.removeItem(SESSION_HINT);
  },
  mayHaveSession: (): boolean => localStorage.getItem(SESSION_HINT) === "1",
};

// Single flight: if five requests get a 401 at the same moment, only ONE refresh
// call goes out and all five wait for it (a second call with the same cookie
// would look like token reuse to the server).
let refreshing: Promise<boolean> | null = null;

/** Get a new access token from the refresh cookie. false = must log in again. */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        const res = await fetch(`${BASE}/auth/refresh`, {
          method: "POST",
          headers: { "X-Requested-With": "fetch" }, // CSRF defence: forms cannot set custom headers
          credentials: "same-origin",
        });
        if (res.ok) {
          tokenStore.set(((await res.json()) as Token).access_token);
          return true;
        }
        // 409: another tab rotated the cookie a moment ago; the browser now has the
        // new cookie, so one retry succeeds.
        if (res.status !== 409) break;
        await new Promise((r) => setTimeout(r, 300));
      }
      tokenStore.clear();
      return false;
    } catch {
      return false; // network error: keep the current state, the next request retries
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

/** Fired on 401 so the app can log out and redirect to /login. */
export const UNAUTHORIZED_EVENT = "documind:unauthorized";

async function parseError(res: Response): Promise<ApiError> {
  let message = `Request failed (${res.status})`;
  let code: string | undefined;
  try {
    const body = await res.json();
    if (body?.error?.message) {
      message = body.error.message;
      code = body.error.code;
    } else if (Array.isArray(body?.detail)) {
      // FastAPI 422 validation errors: [{loc, msg, ...}]
      message = body.detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${String(d.loc?.slice(-1)[0] ?? "field")}: ${d.msg}`)
        .join("; ");
      code = "validation_error";
    }
  } catch {
    /* non-JSON error body */
  }
  if (res.status === 429) {
    const retry = res.headers.get("Retry-After");
    if (retry) message += ` (try again in ${retry}s)`;
  }
  return new ApiError(res.status, message, code);
}

const NO_REFRESH = ["/auth/login", "/auth/register", "/auth/refresh", "/auth/logout"];

/** fetch() with the access token. On 401 the token has probably just expired:
 *  refresh it once and repeat the request. If that fails too -> logged out. */
async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = () => {
    const headers = new Headers(init.headers);
    const token = tokenStore.get();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !(init.body instanceof FormData) && !(init.body instanceof URLSearchParams)) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(`${BASE}${path}`, { ...init, headers, credentials: "same-origin" });
  };
  const res = await send();
  if (res.status !== 401 || NO_REFRESH.includes(path)) return res;
  if (await refreshSession()) {
    const retried = await send();
    if (retried.status !== 401) return retried;
  }
  tokenStore.clear();
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  return res;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authFetch(path, init);
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const qs = (params: Record<string, string | number | undefined>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

// --- endpoints -----------------------------------------------------------------
export const api = {
  // auth
  register: (name: string, email: string, password: string) =>
    request<User>("/auth/register", { method: "POST", body: JSON.stringify({ name, email, password }) }),
  login: (email: string, password: string) =>
    request<Token>("/auth/login", {
      method: "POST",
      body: new URLSearchParams({ username: email, password }), // OAuth2 password form
    }),
  me: () => request<User>("/auth/me"),
  logout: () => request<void>("/auth/logout", { method: "POST", headers: { "X-Requested-With": "fetch" } }),

  // documents
  listDocuments: (params: { limit?: number; offset?: number; status?: DocumentStatus } = {}) =>
    request<Page<DocumentItem>>(`/documents${qs(params)}`),
  getDocument: (id: string) => request<DocumentItem>(`/documents/${id}`),
  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: "DELETE" }),
  reprocessDocument: (id: string) => request<DocumentItem>(`/documents/${id}/reprocess`, { method: "POST" }),
  requestSummary: (id: string) => request<DocumentItem>(`/documents/${id}/summary`, { method: "POST" }),

  // chat & conversations
  ask: (question: string, conversationId?: string, documentIds?: string[]) =>
    request<AskResponse>("/chat/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        conversation_id: conversationId ?? null,
        document_ids: documentIds && documentIds.length ? documentIds : null,
      }),
    }),
  listConversations: (limit = 50, offset = 0, q?: string) =>
    request<Page<Conversation>>(`/conversations${qs({ limit, offset, q: q?.trim() || undefined })}`),
  renameConversation: (id: string, title: string) =>
    request<Conversation>(`/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  getConversation: (id: string) => request<Conversation>(`/conversations/${id}`),
  listMessages: (id: string) => request<Page<Message>>(`/conversations/${id}/messages?limit=200`),
  setFeedback: (conversationId: string, messageId: string, rating: 1 | -1 | null) =>
    request<Message>(`/conversations/${conversationId}/messages/${messageId}/feedback`, {
      method: "PUT", body: JSON.stringify({ rating }),
    }),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, { method: "DELETE" }),
};

/** Download a conversation as Markdown or JSON (needs the auth header -> Blob -> temporary link). */
export async function downloadConversation(id: string, format: "markdown" | "json"): Promise<void> {
  const res = await authFetch(`/conversations/${id}/export${qs({ format })}`);
  if (!res.ok) throw await parseError(res);
  const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? `conversation.${format === "json" ? "json" : "md"}`;
  const url = URL.createObjectURL(await res.blob());
  const link = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The original uploaded file (needs the auth header, so it is fetched as a Blob). */
export async function fetchDocumentFile(id: string): Promise<Blob> {
  const res = await authFetch(`/documents/${id}/file`);
  if (!res.ok) throw await parseError(res);
  return res.blob();
}

// --- Server-Sent Events --------------------------------------------------------
// The browser's EventSource only supports GET without custom headers (no
// Authorization), so streams are read by hand from fetch().body.

/** Yields {event, data} for each SSE event in the response body. Comment lines
 *  (": ping" heartbeats) are skipped. */
async function* readSse(res: Response): AsyncGenerator<{ event: string; data: unknown }> {
  if (!res.body) throw new ApiError(0, "Streaming is not supported by this browser.");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    // Events are separated by a blank line; the last piece may be incomplete.
    const blocks = buffer.split("\n\n");
    buffer = done ? "" : blocks.pop()!;
    for (const block of blocks) {
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) };
    }
    if (done) return;
  }
}

export interface StreamHandlers {
  onMeta?: (meta: { conversation_id: string; conversation_title: string }) => void;
  onToken: (text: string) => void;
}

/** POST /chat/stream. Resolves with the final AskResponse (the `done` event). */
export async function askStream(
  question: string, conversationId: string | undefined, documentIds: string[],
  handlers: StreamHandlers, signal?: AbortSignal, regenerate = false,
): Promise<AskResponse> {
  const res = await authFetch("/chat/stream", {
    method: "POST",
    signal,
    body: JSON.stringify({
      question,
      conversation_id: conversationId ?? null,
      document_ids: documentIds.length ? documentIds : null,
      regenerate,
    }),
  });
  if (!res.ok) throw await parseError(res);
  let result: AskResponse | null = null;
  for await (const { event, data } of readSse(res)) {
    const payload = data as Record<string, string>;
    if (event === "meta") handlers.onMeta?.(data as { conversation_id: string; conversation_title: string });
    else if (event === "token") handlers.onToken(payload.text!);
    else if (event === "done") result = data as AskResponse; // the server closes right after it
    else if (event === "error") throw new ApiError(500, payload.message!);
  }
  if (result) return result;
  throw new ApiError(0, "The connection closed before the answer was complete.");
}

/** One live status update of a document (GET /documents/events). */
export interface DocumentEvent {
  document_id: string;
  status: DocumentStatus;
  pages_done?: number;
  pages_total?: number;
  error_message?: string;
}

/**
 * Follow the user's document status stream until `signal` aborts. The server
 * ends each stream after a few minutes and on errors we back off; either way we
 * reconnect, and every connection starts with a fresh snapshot.
 * `onLive(false)` = no live updates right now (the caller should poll).
 */
export async function watchDocumentEvents(
  onEvent: (e: DocumentEvent) => void,
  onLive: (live: boolean) => void,
  signal: AbortSignal,
): Promise<void> {
  let backoff = 1000;
  while (!signal.aborted) {
    try {
      const res = await authFetch("/documents/events", { signal });
      if (!res.ok) throw await parseError(res);
      for await (const { event, data } of readSse(res)) {
        if (event === "snapshot") {
          const snap = data as { documents: DocumentEvent[]; live: boolean };
          onLive(snap.live);
          snap.documents.forEach(onEvent);
          backoff = 1000;
        } else if (event === "document") {
          onEvent(data as DocumentEvent);
        }
      }
    } catch (err) {
      if (signal.aborted) return;
      onLive(false);
      if (err instanceof ApiError && err.status === 401) return; // logged out
    }
    if (signal.aborted) return;
    await new Promise((r) => setTimeout(r, backoff));
    backoff = Math.min(backoff * 2, 30_000);
  }
}

/**
 * Upload with progress. fetch() cannot report upload progress, so this uses
 * XMLHttpRequest. Resolves with the created document. An expired access token
 * is refreshed once and the upload repeated.
 */
export async function uploadDocument(file: File, onProgress: (fraction: number) => void): Promise<DocumentItem> {
  try {
    return await uploadOnce(file, onProgress);
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) throw err;
    if (!(await refreshSession())) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
      throw err;
    }
    onProgress(0);
    return uploadOnce(file, onProgress);
  }
}

function uploadOnce(file: File, onProgress: (fraction: number) => void): Promise<DocumentItem> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/documents`);
    const token = tokenStore.get();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = async () => {
      const res = new Response(xhr.responseText, {
        status: xhr.status,
        headers: { "Content-Type": "application/json", "Retry-After": xhr.getResponseHeader("Retry-After") ?? "" },
      });
      if (xhr.status >= 200 && xhr.status < 300) resolve((await res.json()) as DocumentItem);
      else reject(await parseError(res));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error - is the server running?"));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}
