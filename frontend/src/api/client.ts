// Typed client for the FastAPI backend. All URLs are relative ("/api/v1/..."):
// the Vite dev server (and nginx in production) proxies them to FastAPI.
import type {
  AskResponse, Conversation, DocumentItem, DocumentStatus, Message, Page, Token, User,
} from "./types";

const BASE = "/api/v1";
const TOKEN_KEY = "documind.token";

// --- token storage -----------------------------------------------------------
// localStorage keeps the user logged in across tabs/reloads. Trade-off: any
// script running on the page can read it (XSS). Mitigations: no third-party
// scripts, React escapes all rendered text, short-lived tokens (60 min), CSP
// headers from nginx. An httpOnly cookie avoids XSS token theft but needs CSRF
// protection - see docs/security.md.
export const tokenStore = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = tokenStore.get();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !(init.body instanceof URLSearchParams)) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (res.status === 401 && token) {
    tokenStore.clear();
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
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

  // documents
  listDocuments: (params: { limit?: number; offset?: number; status?: DocumentStatus } = {}) =>
    request<Page<DocumentItem>>(`/documents${qs(params)}`),
  getDocument: (id: string) => request<DocumentItem>(`/documents/${id}`),
  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: "DELETE" }),
  reprocessDocument: (id: string) => request<DocumentItem>(`/documents/${id}/reprocess`, { method: "POST" }),

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
  listConversations: (limit = 50, offset = 0) => request<Page<Conversation>>(`/conversations${qs({ limit, offset })}`),
  getConversation: (id: string) => request<Conversation>(`/conversations/${id}`),
  listMessages: (id: string) => request<Page<Message>>(`/conversations/${id}/messages?limit=200`),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, { method: "DELETE" }),
};

/**
 * Upload with progress. fetch() cannot report upload progress, so this uses
 * XMLHttpRequest. Resolves with the created document.
 */
export function uploadDocument(file: File, onProgress: (fraction: number) => void): Promise<DocumentItem> {
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
