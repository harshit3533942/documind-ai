"""Browser end-to-end test of the full stack (React UI -> FastAPI -> Celery -> pgvector).

Requires running: PostgreSQL + Redis (docker compose), the API, a Celery worker
and the frontend (Vite dev server or the nginx container).

    pip install playwright==1.49.1 && python -m playwright install chromium
    python -m scripts.e2e_frontend --base http://127.0.0.1:5173 --shots <dir>

Registers a fresh user, uploads a PDF, waits for background processing, asks
questions (incl. a follow-up and an unanswerable one), checks citations,
history, the document library, logout and route protection. Fails on any
browser console error or HTTP 5xx.
"""

import argparse
import json
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.tests.pdf_factory import docx_file, scanned_pdf, text_pdf

PAGES = [
    "Employee Handbook\n\nAll full-time employees receive 24 days of paid annual leave per year. "
    "Employees are entitled to 10 days of paid sick leave.",
    "Remote Work\n\nEmployees may work remotely up to three days per week with manager approval. "
    "The daily meal allowance for domestic travel is INR 1,500.",
]


def latest_link(mailpit: str, to: str, path: str, timeout: float = 30) -> str:
    """The newest link to `path` in an email to `to`, read from Mailpit's API."""
    query = urllib.parse.quote(f'to:"{to}"')
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen(f"{mailpit}/api/v1/search?query={query}", timeout=5) as r:  # noqa: S310 - local test
            messages = json.load(r)["messages"]
        for summary in messages:  # newest first
            with urllib.request.urlopen(f"{mailpit}/api/v1/message/{summary['ID']}", timeout=5) as r:  # noqa: S310
                text = json.load(r)["Text"]
            if match := re.search(rf"(https?://\S+{re.escape(path)}\?token=[\w-]+)", text):
                return match.group(1)
        time.sleep(0.5)
    raise AssertionError(f"no email with a {path} link for {to}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--shots", default=tempfile.gettempdir())
    parser.add_argument("--mailpit", default="http://localhost:8025", help="Mailpit web/API address")
    args = parser.parse_args()
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    email = f"e2e-{int(time.time())}@example.com"
    pdf_path = shots / "e2e-handbook.pdf"
    pdf_path.write_bytes(text_pdf(PAGES))
    # Multi-format upload: Markdown + Word + a scanned (image-only) PDF that needs OCR.
    md_path = shots / "e2e-notes.md"
    md_path.write_text("# Team notes\n\nThe **office opens at 9 am** on weekdays.\n\n- Bring your ID card\n", encoding="utf-8")
    docx_path = shots / "e2e-policy.docx"
    docx_path.write_bytes(docx_file([("h1", "Travel policy"), ("p", "Hotel stays are reimbursed up to INR 4,000 per night.")]))
    scan_path = shots / "e2e-scanned.pdf"
    scan_path.write_bytes(scanned_pdf(["Parking Rules\n\nThe monthly parking fee is INR 300 for every employee."]))
    console_errors: list[str] = []
    server_errors: list[str] = []
    step = 0

    def ok(message: str) -> None:
        nonlocal step
        step += 1
        print(f"  [{step:02}] ok  {message}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        expected_failures = []  # (url suffix, status) the test triggers on purpose

        def on_console(m) -> None:  # noqa: ANN001
            if m.type != "error":
                return
            url = m.location.get("url", "")
            if any(url.endswith(suffix) and str(code) in m.text for suffix, code in expected_failures):
                return  # the browser logs every failed request; this one is the point of the test
            console_errors.append(f"{m.text} @ {url}")

        page.on("console", on_console)
        page.on("response", lambda r: r.status >= 500 and server_errors.append(f"{r.status} {r.url}"))
        expect.set_options(timeout=15_000)

        # 1. route protection
        page.goto(f"{args.base}/documents")
        expect(page).to_have_url(re.compile(r"/login$"))
        ok("unauthenticated visit to /documents redirects to /login")
        page.screenshot(path=shots / "01-login.png")

        # 2. register (auto-login) -> upload page
        page.click("text=Create an account")
        page.get_by_label("Name").fill("E2E Tester")
        page.get_by_label("Email").fill(email)
        page.get_by_label("Password").fill("E2eTest12345")
        page.get_by_role("button", name="Create account").click()
        expect(page).to_have_url(re.compile(r"/upload$"))
        ok(f"registered {email} and landed on /upload")

        # 2b. email verification: banner -> link from the email (Mailpit) -> confirmed
        expect(page.get_by_text("Please confirm your email address")).to_be_visible()
        verify_link = latest_link(args.mailpit, email, "/verify-email")
        page.goto(verify_link)
        expect(page.get_by_text("Your email address is confirmed.")).to_be_visible()
        expect(page).to_have_url(re.compile(r"/verify-email$"))  # token removed from the address bar
        page.get_by_role("link", name="Go to DocuMind").click()
        expect(page.get_by_text("Please confirm your email address")).to_have_count(0)
        page.get_by_label("Main").get_by_role("link", name="Upload").click()
        ok("verification email received; its link confirms the address and the banner disappears")

        # 3. upload -> live status until processed by the Celery worker
        page.set_input_files("[data-testid=file-input]", str(pdf_path))
        expect(page.get_by_text("Ready:")).to_be_visible(timeout=90_000)
        ok("upload -> background processing -> status 'completed' shown live")
        page.screenshot(path=shots / "02-upload.png")

        # 4. ask about this document
        page.get_by_role("link", name=re.compile("Ask about this document")).click()
        expect(page).to_have_url(re.compile(r"/chat\?doc="))
        page.get_by_label("Your question").fill("How many vacation days do I get?")
        page.keyboard.press("Enter")
        answer = page.locator("div.rounded-bl-sm").last
        expect(answer).to_contain_text("24 days", timeout=60_000)  # first question after a restart loads the re-ranker
        expect(answer).to_contain_text("e2e-handbook.pdf")
        expect(answer).to_contain_text("p. 1")
        expect(page).to_have_url(re.compile(r"/chat/[0-9a-f-]{36}$"))
        ok("answer '24 days' with citation e2e-handbook.pdf p. 1; URL now /chat/<conversation id>")

        # 4b. sources -> in-app PDF viewer at the cited page
        answer.get_by_role("button", name="View page").first.click()
        viewer = page.get_by_role("dialog")
        expect(viewer).to_contain_text("Page 1")
        expect(viewer.locator("iframe")).to_have_attribute("src", re.compile(r"^blob:.*#page=1$"))
        page.screenshot(path=shots / "03a-viewer.png")
        viewer.get_by_role("button", name="Close viewer").click()
        expect(viewer).to_have_count(0)
        ok("'View page' opens the PDF viewer at page 1 (blob URL, allowed by the CSP)")

        # 4c. rating survives a reload (also proves the silent login via refresh cookie)
        answer.get_by_role("button", name="Helpful", exact=True).click()
        expect(answer.get_by_role("button", name="Helpful", exact=True)).to_have_attribute("aria-pressed", "true")
        page.wait_for_timeout(500)
        page.reload()
        answer = page.locator("div.rounded-bl-sm").last
        expect(answer.get_by_role("button", name="Helpful", exact=True)).to_have_attribute("aria-pressed", "true")
        ok("👍 saved; after a full reload the user is still logged in and the rating is shown")

        # 4d. regenerate replaces the last answer (still one answer on screen)
        answer.get_by_role("button", name=re.compile("Regenerate")).click()
        expect(page.get_by_role("button", name="Stop generating")).to_have_count(0, timeout=60_000)
        expect(page.locator("div.rounded-bl-sm")).to_have_count(1)
        expect(page.locator("div.rounded-bl-sm").last).to_contain_text("24")
        ok("regenerate produced a fresh answer in place of the old one")

        # 5. follow-up uses the conversation history
        page.get_by_label("Your question").fill("And sick leave?")
        page.keyboard.press("Enter")
        expect(page.locator("div.rounded-bl-sm").nth(1)).to_contain_text("10 days")
        ok("follow-up 'And sick leave?' answered with '10 days'")

        # 6. unanswerable question is refused, not invented
        page.get_by_label("Your question").fill("Who won the 2022 football world cup?")
        page.keyboard.press("Enter")
        expect(page.locator("div.rounded-bl-sm").nth(2)).to_contain_text("do not contain enough information")
        ok("unrelated question -> 'documents do not contain enough information'")
        page.screenshot(path=shots / "03-chat.png", full_page=True)

        # 6b. stop: nothing is saved, the question goes back into the input box
        page.get_by_label("Your question").fill("Summarise the remote work rules in detail")
        page.keyboard.press("Enter")
        page.get_by_role("button", name="Stop generating").click()
        expect(page.get_by_text("Stopped. The partial answer was not saved.")).to_be_visible()
        expect(page.get_by_label("Your question")).to_have_value("Summarise the remote work rules in detail")
        expect(page.locator("div.rounded-bl-sm")).to_have_count(3)
        page.get_by_label("Your question").fill("")
        ok("Stop aborts the stream; nothing saved, question restored to the input")

        # 7. history page lists the conversation; reopening restores messages
        page.get_by_label("Main").get_by_role("link", name="History").click()
        row = page.get_by_text("How many vacation days do I get?").first
        expect(row).to_be_visible()
        expect(page.get_by_text("6 messages")).to_be_visible()
        page.screenshot(path=shots / "04-history.png")
        page.get_by_role("link", name="Continue").first.click()
        expect(page.locator("div.rounded-bl-sm")).to_have_count(3)
        ok("history shows the conversation (6 messages); 'Continue' reloads all 3 answers")

        # 7b. search, rename, export
        page.get_by_label("Main").get_by_role("link", name="History").click()
        search = page.get_by_label("Search conversations")
        search.fill("world cup")  # only inside a QUESTION, not the title
        expect(page.get_by_text(re.compile(r"^Match: .*world cup", re.I))).to_be_visible()
        search.fill("no such words anywhere")
        expect(page.get_by_text('No conversations match "no such words anywhere"')).to_be_visible()
        search.fill("")
        page.get_by_role("button", name="Rename").first.click()
        page.get_by_label("Conversation title").fill("Leave questions")
        page.keyboard.press("Enter")
        expect(page.get_by_text("Leave questions", exact=True)).to_be_visible()
        with page.expect_download() as download_info:
            page.get_by_role("button", name="Export .md").first.click()
        download = download_info.value
        exported = Path(download.path()).read_text(encoding="utf-8")
        assert download.suggested_filename == "documind-leave-questions.md", download.suggested_filename
        assert exported.startswith("# Leave questions") and "24 days" in exported and "e2e-handbook.pdf" in exported
        page.reload()
        expect(page.get_by_text("Leave questions", exact=True)).to_be_visible()
        ok("search finds text inside messages; rename persists; Markdown export downloads with citations")

        # 8. document library + dashboard
        page.get_by_label("Main").get_by_role("link", name="Documents").click()
        expect(page.get_by_text("e2e-handbook.pdf")).to_be_visible()
        expect(page.get_by_text("completed", exact=True)).to_be_visible()
        expect(page.get_by_text(re.compile("^Live: status updates"))).to_be_visible()  # SSE stream connected
        details = page.get_by_text("Summary & suggested questions")
        expect(details).to_be_visible(timeout=60_000)  # written by the worker after processing
        details.click()
        first_question = page.locator("details a").first
        expect(first_question).to_be_visible()
        suggested = first_question.inner_text()
        page.screenshot(path=shots / "05-documents.png")
        first_question.click()
        expect(page).to_have_url(re.compile(r"/chat\?doc=.*&q="))
        expect(page.get_by_label("Your question")).to_have_value(suggested)
        expect(page.get_by_test_id("document-summary")).to_be_visible()
        page.get_by_label("Your question").fill("")
        ok(f"summary generated in the background; suggested question '{suggested}' opens the chat prefilled")
        page.get_by_label("Main").get_by_role("link", name="Documents").click()
        page.get_by_label("Main").get_by_role("link", name="Dashboard").click()
        expect(page.get_by_text("1 ready for questions")).to_be_visible()
        page.screenshot(path=shots / "06-dashboard.png")
        ok("document library shows 'completed' with the live status stream; dashboard shows 1 ready document")

        # 8b. several files at once: Markdown + DOCX (converted to PDF) + scanned PDF (OCR)
        page.get_by_label("Main").get_by_role("link", name="Upload").click()
        page.set_input_files("[data-testid=file-input]", [str(md_path), str(docx_path), str(scan_path)])
        expect(page.get_by_text("Ready:")).to_have_count(3, timeout=180_000)
        page.screenshot(path=shots / "06b-multi-upload.png")
        page.get_by_label("Main").get_by_role("link", name="Documents").click()
        expect(page.get_by_text("docx", exact=True)).to_be_visible()
        expect(page.get_by_text("md", exact=True)).to_be_visible()
        ok("3 files uploaded together: .md and .docx converted, scanned PDF read with OCR - all 'completed'")

        # 8c. the OCR text is searchable
        page.goto(f"{args.base}/chat")
        page.get_by_label("Your question").fill("What is the monthly parking fee?")
        page.keyboard.press("Enter")
        ocr_answer = page.locator("div.rounded-bl-sm").last
        expect(ocr_answer).to_contain_text("300", timeout=60_000)
        expect(ocr_answer).to_contain_text("e2e-scanned.pdf")
        ok("question answered from the OCR text of the scanned PDF ('INR 300', cited e2e-scanned.pdf)")

        # 9. delete the conversations, then the documents
        page.get_by_label("Main").get_by_role("link", name="History").click()
        deletes = page.get_by_role("button", name="Delete")
        expect(deletes.first).to_be_visible()  # list loaded
        for remaining in range(deletes.count(), 0, -1):
            page.once("dialog", lambda d: d.accept())
            deletes.first.click()
            expect(deletes).to_have_count(remaining - 1)
        expect(page.get_by_text("No conversations yet")).to_be_visible()
        page.get_by_label("Main").get_by_role("link", name="Documents").click()
        expect(deletes).to_have_count(4)
        for remaining in range(4, 0, -1):
            page.once("dialog", lambda d: d.accept())
            deletes.first.click()
            expect(deletes).to_have_count(remaining - 1)
        expect(page.get_by_text("No documents yet")).to_be_visible()
        ok("deleted conversations and documents through the UI")

        # 10. logout -> protected again
        page.get_by_role("button", name="Log out").click()
        expect(page).to_have_url(re.compile(r"/login$"))
        page.goto(f"{args.base}/chat")
        expect(page).to_have_url(re.compile(r"/login$"))
        ok("logout clears the session; /chat redirects to /login again")

        # 11. forgot password -> email -> new password -> log in with it
        page.goto(f"{args.base}/login")
        page.get_by_role("link", name="Forgot password?").click()
        page.get_by_label("Email").fill(email)
        page.get_by_role("button", name="Send reset link").click()
        expect(page.get_by_text("If an account exists for this email")).to_be_visible()
        page.goto(latest_link(args.mailpit, email, "/reset-password"))
        expect(page).to_have_url(re.compile(r"/reset-password$"))
        new_password_inputs = page.locator("input[autocomplete=new-password]")
        new_password_inputs.nth(0).fill("NewE2ePassw0rd")
        new_password_inputs.nth(1).fill("NewE2ePassw0rd")
        page.get_by_role("button", name="Set new password").click()
        expect(page.get_by_text("Your password has been changed.")).to_be_visible()
        page.get_by_role("link", name="Log in").last.click()
        page.get_by_label("Email").fill(email)
        page.get_by_label("Password").fill("E2eTest12345")
        expected_failures.append(("/api/v1/auth/login", 401))  # the OLD password must be rejected
        page.get_by_role("button", name="Log in").click()
        expect(page.get_by_text("Incorrect email or password")).to_be_visible()
        page.get_by_label("Password").fill("NewE2ePassw0rd")
        page.get_by_role("button", name="Log in").click()
        expect(page).not_to_have_url(re.compile(r"/login$"))
        page.get_by_role("button", name="Log out").click()
        ok("password reset by email: old password rejected, new password logs in")
        browser.close()

    print(f"\n  console errors: {len(console_errors)}   HTTP 5xx: {len(server_errors)}")
    for e in console_errors + server_errors:
        print("   ", e)
    print(f"  screenshots: {shots}")
    print(f"  test user left in DB: {email}")
    return 1 if console_errors or server_errors else 0


if __name__ == "__main__":
    sys.exit(main())
