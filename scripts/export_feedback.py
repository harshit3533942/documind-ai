"""Turn users' answer ratings into evaluation material.

    python -m scripts.export_feedback --out feedback.json [--since-days 30]

* 👍 answers with citations -> `candidates`: {q, filename, page, ...} in the
  same shape as scripts/eval/dataset.json questions. A human reviews them and
  copies the good ones into an evaluation set (the user said the answer was
  helpful, the cited page is the likely ground truth).
* 👎 answers -> `to_review`: the question, the answer and its sources, to find
  out WHY retrieval or generation failed (wrong page? missing document?).

Operator tool: the output contains users' questions, so treat it as user data
(do not commit it, delete it after review). User ids/emails are NOT exported.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.database.connection import session_scope
from app.models import Message, MessageRole


def collect_feedback(db: Session, *, since: datetime | None = None) -> dict[str, Any]:
    answer = aliased(Message)
    question = aliased(Message)
    # The question of an answer = the latest user message before it in the same conversation.
    question_sq = (
        select(question.content)
        .where(
            question.conversation_id == answer.conversation_id,
            question.role == MessageRole.USER,
            question.created_at <= answer.created_at,
        )
        .order_by(question.created_at.desc(), question.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = select(answer, question_sq).where(answer.role == MessageRole.ASSISTANT, answer.feedback.is_not(None))
    if since is not None:
        stmt = stmt.where(answer.created_at >= since)

    candidates: list[dict[str, Any]] = []
    to_review: list[dict[str, Any]] = []
    for msg, q in db.execute(stmt.order_by(answer.created_at)).all():
        citations = (msg.sources or {}).get("citations", [])
        if msg.feedback == 1 and citations:
            top = citations[0]
            candidates.append({"q": q, "filename": top["filename"], "page": top["page_start"],
                               "kind": "user-feedback", "answer": msg.content})
        elif msg.feedback == -1:
            to_review.append({"q": q, "answer": msg.content,
                              "sources": [{"filename": c["filename"], "page": c["page_start"]} for c in citations]})
    return {"generated_at": datetime.now(UTC).isoformat(), "candidates": candidates, "to_review": to_review}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--since-days", type=int, default=None)
    args = parser.parse_args()
    since = datetime.now(UTC) - timedelta(days=args.since_days) if args.since_days else None
    with session_scope() as db:
        data = collect_feedback(db, since=since)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(data['candidates'])} candidate eval questions, {len(data['to_review'])} answers to review -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
