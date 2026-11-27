"""Semantic answer cache: reuse answers of questions that MEAN the same."""

import json

import pytest

from app.core.config import get_settings
from app.services import cache_service
from app.services.embedding_service import HashingEmbedder
from app.tests.test_chat import ask, setup_user  # noqa: F401 - fixture re-used

# Questions that the scripted embedder treats as the same meaning (same vector).
SAME_MEANING = {
    "How many vacation days do I get?": "annual leave days",
    "How many days of vacation do I get?": "annual leave days",
    "What was the revenue in 2023?": "revenue",
    "What was the revenue in 2024?": "revenue",
    "Is remote work allowed?": "remote work",
    "Is remote work not allowed?": "remote work",
}


class ScriptedEmbedder(HashingEmbedder):
    """Paraphrases from SAME_MEANING get identical query vectors (like a real
    model would give near-identical ones); everything else is hashed."""

    def embed_query(self, text: str) -> list[float]:
        return super().embed_query(SAME_MEANING.get(text, text))


@pytest.fixture
def fake_embedder() -> ScriptedEmbedder:  # overrides the conftest fixture used by `client`
    return ScriptedEmbedder()


@pytest.fixture(autouse=True)
def semantic_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "semantic_cache_enabled", True)


def answer(client, h, question: str, **extra) -> dict:
    resp = ask(client, h, question, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ hits
def test_paraphrase_reuses_the_answer(client, setup_user, fake_redis) -> None:  # noqa: F811
    _, h, _ = setup_user()
    first = answer(client, h, "How many vacation days do I get?")
    second = answer(client, h, "How many days of vacation do I get?")
    assert first["cached"] is False
    assert second["cached"] is True and second["cache_similarity"] >= 0.99
    assert second["assistant_message"]["content"] == first["assistant_message"]["content"]
    assert second["assistant_message"]["citations"] == first["assistant_message"]["citations"]
    stats = fake_redis.hgetall("metrics:cache")
    assert stats["semantic_hit"] == "1" and stats["semantic_miss"] == "1"


def test_streaming_uses_the_semantic_cache_too(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    answer(client, h, "How many vacation days do I get?")
    resp = client.post("/api/v1/chat/stream", headers=h, json={"question": "How many days of vacation do I get?"})
    done = [line for line in resp.text.split("\n\n") if line.startswith("event: done")][0]
    body = json.loads(done.split("data: ", 1)[1])
    assert body["cached"] is True and body["cache_similarity"] >= 0.99


# ------------------------------------------------------------------ guards
@pytest.mark.parametrize(("first", "second"), [
    ("What was the revenue in 2023?", "What was the revenue in 2024?"),  # same vector, different number
    ("Is remote work allowed?", "Is remote work not allowed?"),          # same vector, negation
    ("How many vacation days do I get?", "What is the travel policy?"),  # different meaning
])
def test_no_hit_for_different_questions(client, setup_user, first, second) -> None:  # noqa: F811
    _, h, _ = setup_user()
    answer(client, h, first)
    assert answer(client, h, second)["cached"] is False


def test_scope_user_documents_and_version(client, setup_user, db_session, storage) -> None:  # noqa: F811
    _, alice, ids = setup_user("alice@example.com", docs={"handbook.pdf": ["Annual leave is 24 days.",
                                                                        "Travel is economy class."],
                                                          "other.pdf": ["Annual leave rules for contractors."]})
    answer(client, alice, "How many vacation days do I get?")
    # another user: never served from alice's cache
    _, bob, _ = setup_user("bob@example.com")
    assert answer(client, bob, "How many days of vacation do I get?")["cached"] is False
    # another document selection: other scope
    only_other = [str(ids["other.pdf"])]
    assert answer(client, alice, "How many days of vacation do I get?", document_ids=only_other)["cached"] is False
    # documents changed (trash) -> new version -> old entries are never read
    client.delete(f"/api/v1/documents/{ids['other.pdf']}", headers=alice)
    assert answer(client, alice, "How many days of vacation do I get?")["cached"] is False


def test_regenerate_skips_the_cache(client, setup_user) -> None:  # noqa: F811
    _, h, _ = setup_user()
    answer(client, h, "How many vacation days do I get?")
    conv = answer(client, h, "How many days of vacation do I get?")
    assert conv["cached"] is True
    again = answer(client, h, "How many days of vacation do I get?",
                   conversation_id=conv["conversation_id"], regenerate=True)
    assert again["cached"] is False


def test_disabled_and_expired_answers(client, setup_user, fake_redis, monkeypatch) -> None:  # noqa: F811
    _, h, _ = setup_user()
    answer(client, h, "How many vacation days do I get?")
    for key in fake_redis.scan_iter("ans:*"):
        fake_redis.delete(key)  # the answer expired, the semantic entry still points to it
    assert answer(client, h, "How many days of vacation do I get?")["cached"] is False

    monkeypatch.setattr(get_settings(), "semantic_cache_enabled", False)
    answer(client, h, "What was the revenue in 2023?")
    fake_redis.delete(*list(fake_redis.scan_iter("ans:*")))
    assert not list(fake_redis.scan_iter("sem:*")) or all(
        "revenue" not in e for k in fake_redis.scan_iter("sem:*") for e in fake_redis.lrange(k, 0, -1))


# ------------------------------------------------------------------ unit
def test_same_facts() -> None:
    assert cache_service.same_facts("leave in 2023?", "How much leave in 2023")
    assert not cache_service.same_facts("revenue 2023", "revenue 2024")
    assert not cache_service.same_facts("price 1,500", "price 1,200")
    assert not cache_service.same_facts("is it allowed", "isn't it allowed")
    assert not cache_service.same_facts("with approval", "without approval")


def test_store_is_bounded_and_lookup_picks_the_best(fake_redis, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "semantic_cache_max_entries", 3)
    for i in range(5):
        cache_service.semantic_store("sem:test", "same question", [1.0, float(i), 0.0], f"ans:{i}")
    assert fake_redis.llen("sem:test") == 3                      # oldest entries dropped
    match = cache_service.semantic_lookup("sem:test", "same question", [1.0, 4.0, 0.0])
    assert match.answer_key == "ans:4" and match.similarity == pytest.approx(1.0)
    assert cache_service.semantic_lookup("sem:test", "q", [0.0, 0.0, 1.0]) is None  # below threshold
    assert cache_service.semantic_lookup("sem:empty", "q", [1.0, 0.0, 0.0]) is None


def test_redis_errors_are_a_miss(monkeypatch) -> None:
    from redis.exceptions import RedisError

    from app.database import redis_client

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RedisError("down")
            return fail

    monkeypatch.setattr(redis_client, "get_redis", lambda: Down())
    assert cache_service.semantic_lookup("sem:x", "q", [1.0]) is None
    cache_service.semantic_store("sem:x", "q", [1.0], "ans:x")  # no exception
