"""Conversation export: Markdown (to read / share) and JSON (to process).

The export is a snapshot of what the user saw: questions, answers and the
cited sources (file + pages), taken from the citation snapshot stored with
each answer - so it stays correct even if a cited document was deleted later.
"""

import re
import unicodedata
from datetime import datetime
from typing import Any

from app.models import Conversation, Message, MessageRole
from app.schemas.chat import MessageRead


def export_filename(title: str, extension: str) -> str:
    """ASCII-only, safe for a Content-Disposition header on every OS."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-").lower()[:60].rstrip("-")
    return f"documind-{slug or 'conversation'}.{extension}"


def _when(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _pages(start: int, end: int) -> str:
    return f"p. {start}" if start == end else f"pp. {start}-{end}"


def to_markdown(conversation: Conversation, messages: list[Message]) -> str:
    lines = [f"# {conversation.title}", "", f"_Exported from DocuMind AI - started {_when(conversation.created_at)}_", ""]
    for message in messages:
        read = MessageRead.from_message(message)
        if message.role == MessageRole.USER:
            lines += ["---", "", f"**Question** ({_when(message.created_at)})", "", f"> {read.content}", ""]
            continue
        lines += ["**Answer**", "", read.content, ""]
        if read.citations:
            lines.append("Sources:")
            lines += [
                f"- [{c.number}] {c.filename}, {_pages(c.page_start, c.page_end)}: \"{c.excerpt.strip()}\""
                for c in read.citations
            ]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json(conversation: Conversation, messages: list[Message]) -> dict[str, Any]:
    return {
        "conversation": {
            "id": str(conversation.id),
            "title": conversation.title,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
        },
        "messages": [MessageRead.from_message(m).model_dump(mode="json") for m in messages],
    }
