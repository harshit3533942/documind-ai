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
import re
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.tests.pdf_factory import text_pdf

PAGES = [
    "Employee Handbook\n\nAll full-time employees receive 24 days of paid annual leave per year. "
    "Employees are entitled to 10 days of paid sick leave.",
    "Remote Work\n\nEmployees may work remotely up to three days per week with manager approval. "
    "The daily meal allowance for domestic travel is INR 1,500.",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--shots", default=tempfile.gettempdir())
    args = parser.parse_args()
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    email = f"e2e-{int(time.time())}@example.com"
    pdf_path = shots / "e2e-handbook.pdf"
    pdf_path.write_bytes(text_pdf(PAGES))
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
        page.on("console", lambda m: m.type == "error" and console_errors.append(m.text))
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
        expect(answer).to_contain_text("24 days")
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
        page.get_by_role("link", name="History").click()
        row = page.get_by_text("How many vacation days do I get?").first
        expect(row).to_be_visible()
        expect(page.get_by_text("6 messages")).to_be_visible()
        page.screenshot(path=shots / "04-history.png")
        page.get_by_role("link", name="Continue").first.click()
        expect(page.locator("div.rounded-bl-sm")).to_have_count(3)
        ok("history shows the conversation (6 messages); 'Continue' reloads all 3 answers")

        # 8. document library + dashboard
        page.get_by_role("link", name="Documents").click()
        expect(page.get_by_text("e2e-handbook.pdf")).to_be_visible()
        expect(page.get_by_text("completed", exact=True)).to_be_visible()
        page.screenshot(path=shots / "05-documents.png")
        page.get_by_role("link", name="Dashboard").click()
        expect(page.get_by_text("1 ready for questions")).to_be_visible()
        page.screenshot(path=shots / "06-dashboard.png")
        ok("document library shows 'completed'; dashboard shows 1 ready document")

        # 9. delete the conversation, then the document
        page.get_by_role("link", name="History").click()
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Delete").first.click()
        expect(page.get_by_text("No conversations yet")).to_be_visible()
        page.get_by_role("link", name="Documents").click()
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Delete").first.click()
        expect(page.get_by_text("No documents yet")).to_be_visible()
        ok("deleted conversation and document through the UI")

        # 10. logout -> protected again
        page.get_by_role("button", name="Log out").click()
        expect(page).to_have_url(re.compile(r"/login$"))
        page.goto(f"{args.base}/chat")
        expect(page).to_have_url(re.compile(r"/login$"))
        ok("logout clears the session; /chat redirects to /login again")
        browser.close()

    print(f"\n  console errors: {len(console_errors)}   HTTP 5xx: {len(server_errors)}")
    for e in console_errors + server_errors:
        print("   ", e)
    print(f"  screenshots: {shots}")
    print(f"  test user left in DB: {email}")
    return 1 if console_errors or server_errors else 0


if __name__ == "__main__":
    sys.exit(main())
