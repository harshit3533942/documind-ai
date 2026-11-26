# Testing

**285 backend tests** (pytest) · **97 % coverage of application code** ·
browser end-to-end test (Playwright) · CI on every push (GitHub Actions).

```powershell
docker compose up -d postgres redis        # tests use a separate documind_test database
pytest                                     # everything (~2 min, loads the real model once)
pytest -m "not model"                      # fast run without the real embedding model (~1 min)
pytest --cov=app --cov-report=term-missing # coverage
python -m scripts.e2e_frontend             # browser E2E (needs API + worker + frontend running)
```

## How the tests are built

| Technique | Why |
|---|---|
| Real PostgreSQL + pgvector (`documind_test` DB) | Constraints, cascades, JSONB and vector search behave exactly as in production; SQLite would hide bugs |
| Each test in a transaction that is rolled back | Isolation and speed — no cleanup code, no test can see another test's data |
| Real PDFs generated with PyMuPDF (`pdf_factory.py`) | Text, blank, scanned-style, encrypted and corrupt files without committing binaries |
| `HashingEmbedder` (fake) + `FakeLLM` (scripted) | Deterministic, millisecond-fast; no network, no API cost |
| Real-model tests (`-m model`) | Guard the calibrated thresholds and the tokenizer thread-safety fix |
| `fakeredis` per test | Cache / rate-limit tests are isolated from the real Redis |
| Celery tasks via `.apply()` | Retries executed synchronously → deterministic retry tests without a broker |
| SQL statement counting | Proves the conversation list has no N+1 queries |
| Mocked HTTP (`httpx.MockTransport`) | OpenAI client request format, errors and timeouts without network |
| "Prove the test catches the bug" | Regression tests were run against the broken code first (kombu/redis mismatch, tokenizer race) |

## Spec requirement → tests

| Area | Requirement | Test(s) |
|---|---|---|
| **Auth** | Registration | `test_auth.py::test_register_success`, `…normalises_email`, `…validation_errors` |
| | Login | `test_login_success_returns_valid_jwt`, `test_login_is_case_insensitive_for_email` |
| | Invalid credentials | `test_login_wrong_password_and_unknown_email_look_identical`, `test_login_inactive_user_rejected` |
| | Token attacks | `test_me_rejects_expired_token`, `…_other_secret`, `…_alg_none_token`, `test_role_claim_in_token_is_not_trusted` |
| **Documents** | Upload PDF | `test_documents.py::test_upload_pdf_success` |
| | Invalid file | `test_upload_rejections_leave_no_files` (wrong extension, fake magic bytes, wrong type, empty, corrupt, encrypted) |
| | Empty PDF | `test_extract_blank_pdf_raises_no_text`, `test_scanned_pdf_marked_failed_with_message` |
| | Document deletion | `test_delete_removes_row_chunks_and_file` |
| | Ownership validation | `test_other_user_cannot_access_document`, `test_list_only_returns_own_documents`, `test_chunks_endpoint_enforces_ownership` |
| **RAG** | Embedding generation | `test_embeddings.py::test_every_chunk_gets_a_normalised_vector`, `test_embedding_model.py::*` |
| | Vector retrieval | `test_search_ranks_relevant_chunk_first`, `test_search_never_returns_other_users_chunks` |
| | Context construction | `test_context_respects_token_budget`, `test_prompt_injection_in_document_cannot_break_delimiters` |
| | Citation generation | `test_llm_answer_with_real_citations`, `test_invalid_citation_numbers_are_removed`, `test_page_numbers_never_come_from_llm_text`, `test_chunk_sentences_exact_pages_and_cross_page_merge` |
| | No relevant information | `test_irrelevant_question_is_refused_without_llm`, `test_unanswerable_questions_are_refused` (real model) |
| **Chat** | Ask question | `test_chat.py::test_ask_creates_conversation_and_saves_both_messages` |
| | Conversation history | `test_continue_conversation_and_follow_up_uses_history`, `test_list_conversations_most_recent_first_with_preview` |
| | Unauthorized conversation access | `test_other_user_cannot_touch_conversation` |
| **Background tasks** | Successful processing | `test_background_tasks.py::test_task_processes_document_end_to_end` |
| | Failed processing | `test_permanent_failure_is_not_retried`, `test_timeout_is_not_retried` |
| | Retry handling | `test_transient_error_is_retried_then_succeeds`, `test_gives_up_after_max_retries`, `test_retry_waits_with_exponential_backoff` |
| | Duplicate processing prevention | `test_duplicate_delivery_does_not_duplicate_chunks`, `test_reprocessing_reuses_existing_embeddings`, `test_stale_processing_document_is_taken_over` |
| **Performance** | Cache + invalidation, rate limits | `test_performance.py::*` |
| **Full stack** | UI → API → worker → DB | `scripts/e2e_frontend.py` (10 steps) |

## Bugs the tests found during development

| Bug | Found by |
|---|---|
| Rejected corrupt PDF could not be deleted on Windows (exception chain kept the file handle open) | `test_upload_rejections_leave_no_files` |
| Space inserted into split giant words; token counter treated 9,000-char blobs as 1 token | `test_giant_single_word_keeps_every_character` |
| Method named `list` shadowed the builtin in type annotations | import error in test collection |
| Stale error message shown next to `queued` after a retry | `test_owner_can_reprocess_failed_document` |
| kombu 5.7 requires redis-py ≥ 6.1 — enqueue silently failed | live run → `test_celery_can_really_talk_to_redis` |
| Question and answer got identical timestamps (`now()` = transaction start) | `test_messages_have_stable_chronological_order`, flaky ordering test |
| HuggingFace tokenizer not thread-safe (`Already borrowed`) | concurrent benchmark → `test_concurrent_use_from_many_threads` |
| Mobile horizontal overflow (grid items without `min-w-0`) | Playwright overflow check at 360–1280 px |
