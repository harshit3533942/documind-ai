"""Conversation rename, search and export."""

import json

import pytest
from sqlalchemy import update

from app.models import Conversation, Message
from app.repositories.conversation_repository import like_pattern, snippet
from app.services.export_service import export_filename
from app.tests.test_chat import CONV, ask, setup_user  # noqa: F401 - fixture re-used


# ------------------------------------------------------------------ rename
def test_rename_keeps_list_position(client, setup_user, db_session) -> None:  # noqa: F811
    _, h, _ = setup_user()
    old = ask(client, h, "What is the annual leave policy?").json()["conversation_id"]
    new = ask(client, h, "What is the travel policy?").json()["conversation_id"]

    resp = client.patch(f"{CONV}/{old}", headers=h, json={"title": "  Leave\n questions  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Leave questions"  # whitespace normalised
    assert resp.json()["message_count"] == 2
    # Renaming is not activity: the newer conversation stays on top.
    assert [c["id"] for c in client.get(CONV, headers=h).json()["items"]] == [new, old]


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_rename_validation(client, setup_user, title) -> None:  # noqa: F811
    _, h, _ = setup_user()
    conv = ask(client, h, "annual leave").json()["conversation_id"]
    assert client.patch(f"{CONV}/{conv}", headers=h, json={"title": title}).status_code == 422


def test_rename_other_users_conversation_is_404(client, setup_user) -> None:  # noqa: F811
    _, alice, _ = setup_user("alice@example.com")
    conv = ask(client, alice, "annual leave").json()["conversation_id"]
    _, bob, _ = setup_user("bob@example.com")
    assert client.patch(f"{CONV}/{conv}", headers=bob, json={"title": "mine now"}).status_code == 404
    assert client.get(f"{CONV}/{conv}", headers=alice).json()["title"] == "annual leave"


# ------------------------------------------------------------------ search
def test_search_in_titles_and_messages(client, setup_user, db_session) -> None:  # noqa: F811
    _, h, _ = setup_user()
    leave = ask(client, h, "What is the annual leave policy?").json()["conversation_id"]
    reply = ask(client, h, "Tell me about flights").json()
    travel = reply["conversation_id"]
    db_session.execute(
        update(Message).where(Message.id == reply["assistant_message"]["id"])
        .values(content="Trips under six hours must use Economy Class flights.")
    )

    by_title = client.get(CONV, headers=h, params={"q": "ANNUAL"}).json()
    assert [c["id"] for c in by_title["items"]] == [leave] and by_title["total"] == 1

    # "Economy class" only appears in the ANSWER text of the travel conversation.
    by_message = client.get(CONV, headers=h, params={"q": "economy class"}).json()
    assert [c["id"] for c in by_message["items"]] == [travel]
    assert "economy class" in by_message["items"][0]["match_snippet"].lower()

    assert client.get(CONV, headers=h, params={"q": "nothing like this"}).json()["total"] == 0
    assert client.get(CONV, headers=h, params={"q": "   "}).json()["total"] == 2  # blank = no filter
    plain = client.get(CONV, headers=h).json()["items"][0]
    assert plain["match_snippet"] is None


def test_search_wildcards_are_literal(client, setup_user, db_session) -> None:  # noqa: F811
    _, h, _ = setup_user()
    conv = ask(client, h, "annual leave").json()["conversation_id"]
    db_session.execute(update(Conversation).where(Conversation.title == "annual leave").values(title="50% off_deal"))
    assert client.get(CONV, headers=h, params={"q": "%"}).json()["total"] == 1   # only the title with a real %
    assert client.get(CONV, headers=h, params={"q": "0% off_"}).json()["items"][0]["id"] == conv
    assert client.get(CONV, headers=h, params={"q": "_"}).json()["total"] == 1
    ask(client, h, "travel policy")
    assert client.get(CONV, headers=h, params={"q": "%"}).json()["total"] == 1   # % is not "match everything"


def test_search_is_scoped_to_the_user(client, setup_user) -> None:  # noqa: F811
    _, alice, _ = setup_user("alice@example.com")
    ask(client, alice, "What is the annual leave policy?")
    _, bob, _ = setup_user("bob@example.com")
    assert client.get(CONV, headers=bob, params={"q": "annual"}).json()["total"] == 0


def test_like_pattern_and_snippet() -> None:
    assert like_pattern("50%_\\x") == "%50\\%\\_\\\\x%"
    text = "a" * 100 + " needle " + "b" * 100
    s = snippet(text, "NEEDLE", width=10)
    assert s.startswith("…") and s.endswith("…") and "needle" in s and len(s) < 40
    assert snippet("short text", "absent", width=3) == "short "


# ------------------------------------------------------------------ export
def test_export_markdown_with_citations(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    conv = ask(client, h, "How many days of paid annual leave?").json()["conversation_id"]
    resp = client.get(f"{CONV}/{conv}/export", headers=h)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["content-disposition"] == 'attachment; filename="documind-how-many-days-of-paid-annual-leave.md"'
    md = resp.text
    assert md.startswith("# How many days of paid annual leave?")
    assert "> How many days of paid annual leave?" in md
    assert "**Answer**" in md and "24 days" in md
    assert "- [1] handbook.pdf, p. 1:" in md


def test_export_json(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    conv = ask(client, h, "How many days of paid annual leave?").json()["conversation_id"]
    resp = client.get(f"{CONV}/{conv}/export", headers=h, params={"format": "json"})
    assert resp.headers["content-type"] == "application/json"
    data = json.loads(resp.content)
    assert data["conversation"]["id"] == conv
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][1]["citations"][0]["filename"] == "handbook.pdf"


def test_export_is_owner_only_and_validates_format(client, setup_user) -> None:  # noqa: F811
    _, alice, _ = setup_user("alice@example.com")
    conv = ask(client, alice, "annual leave").json()["conversation_id"]
    _, bob, _ = setup_user("bob@example.com")
    assert client.get(f"{CONV}/{conv}/export", headers=bob).status_code == 404
    assert client.get(f"{CONV}/{conv}/export", headers=alice, params={"format": "pdf"}).status_code == 422


def test_export_filename_is_safe() -> None:
    assert export_filename('Résumé: "Q3" / plan?', "md") == "documind-resume-q3-plan.md"
    assert export_filename("छुट्टी नीति", "json") == "documind-conversation.json"
    assert export_filename("x" * 300, "md") == "documind-" + "x" * 60 + ".md"
