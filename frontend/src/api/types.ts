// Mirrors the FastAPI Pydantic schemas (app/schemas/*.py).

export type Role = "user" | "admin";
export type DocumentStatus = "uploaded" | "queued" | "processing" | "completed" | "failed";

export interface User {
  id: string;
  name: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface Token {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface DocumentItem {
  id: string;
  filename: string;
  file_size: number;
  mime_type: string;
  source_type: "pdf" | "docx" | "txt" | "md"; // the stored file is always a PDF
  status: DocumentStatus;
  total_pages: number | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  processed_at: string | null;
  summary?: string | null; // generated in the background after processing
  suggested_questions?: string[] | null;
  summarized_at?: string | null;
}

export interface Citation {
  number: number;
  document_id: string;
  filename: string;
  page_start: number;
  page_end: number;
  chunk_page_start: number;
  chunk_page_end: number;
  excerpt: string;
  score: number;
  chunk_id: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  citations: Citation[];
  found: boolean | null;
  mode: "llm" | "extractive" | "none" | null;
  grounded: boolean | null;
  document_ids: string[] | null;
  feedback: 1 | -1 | null; // the user's rating of an answer
}

export interface AskResponse {
  conversation_id: string;
  conversation_title: string;
  user_message: Message;
  assistant_message: Message;
  search_query: string;
  timings_ms: Record<string, number>;
  cached: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview: string | null;
  last_message_at: string | null;
  match_snippet?: string | null; // only when searching
}

export const ACTIVE_STATUSES: DocumentStatus[] = ["uploaded", "queued", "processing"];
