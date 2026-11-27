"""Load test for DocuMind AI (Locust).

Every simulated user behaves like a real one: registers, uploads a small
document, waits until the worker has processed it, then keeps asking
questions (some repeated -> answer cache, some paraphrased -> semantic cache,
some new) and browsing the library / history.

Self-contained on purpose (no project imports): it runs in the official Locust
Docker image, against the stack through nginx - exactly the path real users take.

Run (see docs/load-testing.md for the full recipe):

    # the target must not rate-limit the test client or call a paid LLM
    $env:RATE_LIMIT_ENABLED="false"; $env:LLM_PROVIDER="extractive"; docker compose up -d api worker

    docker run --rm --network documindai_default -v "${PWD}/scripts/load:/mnt/locust" `
      locustio/locust:2.47.0 -f /mnt/locust/locustfile.py --host http://frontend `
      --headless -u 20 -r 2 -t 3m --csv /mnt/locust/results/run

Options (environment variables): LOAD_PASSWORD, LOAD_KEEP_DATA=1 (do not delete
the uploaded documents at the end).
"""

import os
import random
import time
import uuid

from locust import HttpUser, between, events, task

API = "/api/v1"
PASSWORD = os.getenv("LOAD_PASSWORD", "LoadTest12345")
KEEP_DATA = os.getenv("LOAD_KEEP_DATA") == "1"
PROCESSING_TIMEOUT_S = 180

HANDBOOK = """Employee Handbook

All full-time employees receive 24 days of paid annual leave per year. Unused leave can be carried over up to 5 days.

Employees are entitled to 10 days of paid sick leave. A medical certificate is required after 3 consecutive days.

Remote Work

Employees may work remotely up to three days per week with manager approval.

Travel Policy

Economy class flights are mandatory for trips under six hours. The daily meal allowance for domestic travel is INR 1,500.
Hotel stays are reimbursed up to INR 4,000 per night.

Notice Period

The notice period is 30 days for employees and 60 days for managers.
"""

# Repeated questions hit the exact cache, paraphrases the semantic cache,
# the rest are full RAG answers.
QUESTIONS = [
    "How many days of paid annual leave do I get?",
    "How many days of paid annual leave do I get?",
    "How many paid annual leave days do I get?",
    "How many days of sick leave are employees entitled to?",
    "Can I work remotely?",
    "What is the daily meal allowance for domestic travel?",
    "What is the hotel limit per night?",
    "What is the notice period for managers?",
    "Who won the football world cup?",  # unanswerable -> "not enough information"
]


class DocuMindUser(HttpUser):
    wait_time = between(1, 4)  # think time between actions

    def on_start(self) -> None:
        self.email = f"load-{uuid.uuid4().hex[:12]}@example.com"
        self.document_id: str | None = None
        self.conversation_id: str | None = None
        self.client.post(f"{API}/auth/register", json={"name": "Load Test", "email": self.email, "password": PASSWORD},
                         name="auth: register")
        resp = self.client.post(f"{API}/auth/login", data={"username": self.email, "password": PASSWORD},
                                name="auth: login")
        token = resp.json().get("access_token") if resp.ok else None
        if not token:
            raise RuntimeError(f"login failed ({resp.status_code}) - is RATE_LIMIT_ENABLED=false on the target?")
        self.client.headers["Authorization"] = f"Bearer {token}"
        self._upload_and_wait()

    def _upload_and_wait(self) -> None:
        text = HANDBOOK + f"\nReference {uuid.uuid4()}\n"  # unique content: no duplicate-upload 409
        resp = self.client.post(f"{API}/documents", files={"file": ("handbook.txt", text.encode(), "text/plain")},
                                name="documents: upload")
        if resp.status_code != 201:
            return
        self.document_id = resp.json()["id"]
        started = time.monotonic()
        with self.client.rename_request("documents: status (poll)"):
            while time.monotonic() - started < PROCESSING_TIMEOUT_S:
                status = self.client.get(f"{API}/documents/{self.document_id}/status").json().get("status")
                if status in ("completed", "failed"):
                    break
                time.sleep(1)
        events.request.fire(  # custom metric: upload -> searchable, as the user experiences it
            request_type="PIPELINE", name="upload -> completed", response_time=(time.monotonic() - started) * 1000,
            response_length=0, exception=None if status == "completed" else RuntimeError(status), context={},
        )

    def on_stop(self) -> None:
        if self.document_id and not KEEP_DATA:
            self.client.delete(f"{API}/documents/{self.document_id}?permanent=true", name="documents: delete")

    # ------------------------------------------------------------------ behaviour
    @task(6)
    def ask_question(self) -> None:
        question = random.choice(QUESTIONS)
        new_conversation = self.conversation_id is None or random.random() < 0.5
        body = {"question": question, "conversation_id": None if new_conversation else self.conversation_id}
        with self.client.post(f"{API}/chat/ask", json=body, name="chat: ask", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"{resp.status_code}: {resp.text[:120]}")
                return
            data = resp.json()
            self.conversation_id = data["conversation_id"]
            if data.get("cached"):
                events.request.fire(request_type="CACHE", name="semantic hit" if data.get("cache_similarity") else "exact hit",
                                    response_time=data["timings_ms"].get("total", 0), response_length=0,
                                    exception=None, context={})

    @task(2)
    def list_documents(self) -> None:
        self.client.get(f"{API}/documents?limit=20", name="documents: list")

    @task(2)
    def list_conversations(self) -> None:
        self.client.get(f"{API}/conversations?limit=20", name="conversations: list")

    @task(1)
    def search_conversations(self) -> None:
        self.client.get(f"{API}/conversations?q=leave", name="conversations: search")

    @task(1)
    def usage(self) -> None:
        self.client.get(f"{API}/account/usage", name="account: usage")
