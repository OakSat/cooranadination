"""Build the extraction prompt: a frozen system prompt plus the delimited, untrusted thread."""

import hashlib
from datetime import date
from functools import cache
from importlib.resources import files

from freight.domain.thread import ThreadMessage

PROMPT_VERSION = "extract_v1"


@cache
def system_prompt(version: str = PROMPT_VERSION) -> str:
    return files("freight.extraction.prompts").joinpath(f"{version}.md").read_text("utf-8")


def escape(s: str) -> str:
    # Only angle brackets: enough to stop an email from closing our delimiter and posing as a
    # second message, while keeping quotes and "&" verbatim for the provenance check.
    return s.replace("<", "&lt;").replace(">", "&gt;")


def _email_block(m: ThreadMessage) -> str:
    parts = [
        f'<email id="{escape(m.email_id)}" direction="{m.direction.value}" '
        f'sent_at="{m.sent_at.isoformat()}">',
        f"<subject>{escape(m.subject)}</subject>",
        escape(m.text),
    ]
    for i, att in enumerate(m.attachments_text, start=1):
        parts.append(f'<attachment n="{i}">\n{escape(att)}\n</attachment>')
    parts.append("</email>")
    return "\n".join(parts)


def build_prompt(thread: list[ThreadMessage], today: date) -> str:
    blocks = "\n\n".join(_email_block(m) for m in sorted(thread, key=lambda m: m.sent_at))
    return f"Today's date: {today.isoformat()}\n\n{blocks}\n\nFill in the shipment form."


def cache_key(model_id: str, prompt: str, version: str = PROMPT_VERSION) -> str:
    digest = hashlib.sha256(f"{model_id}\n{version}\n{prompt}".encode()).hexdigest()
    return f"LLM#{digest}"
