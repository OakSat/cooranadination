"""Check that every quote the model cites really is in the email it claims.

"Valid JSON" is not "correct": a model can produce a well-formed source that it made up. A
value whose evidence is not found verbatim is downgraded to inferred with capped confidence, so
the sensitivity policy and the rep see it as unverified.
"""

import html
import re
import unicodedata

from freight.domain.thread import Direction, ThreadMessage
from freight.extraction.schema import FIELD_NAMES, LLMSource, ShipmentLLMOutput

UNVERIFIED_CONFIDENCE_CAP = 0.5
_WS = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    return _WS.sub(" ", s).strip()


def _locate(quote: str, thread: dict[str, str], claimed: str) -> str | None:
    """Return the id of the email that contains the quote, preferring the claimed one."""
    q = normalize_text(html.unescape(quote))
    if not q:
        return None
    if claimed in thread and q in thread[claimed]:
        return claimed
    for email_id, text in thread.items():
        if q in text:
            return email_id
    return None


class ProvenanceReport:
    def __init__(self) -> None:
        self.downgraded: list[str] = []
        self.repointed: list[str] = []


def verify(output: ShipmentLLMOutput, thread: list[ThreadMessage]) -> ProvenanceReport:
    """Fix ``output`` in place (drop unfound quotes, downgrade) and report what changed."""
    report = ProvenanceReport()
    # Only customer emails count as evidence: our own questions are not the customer's answer.
    texts = {
        m.email_id: normalize_text(m.searchable) for m in thread if m.direction is Direction.INBOUND
    }
    for name in FIELD_NAMES:
        field = getattr(output, name)
        if field.status == "missing":
            continue
        kept: list[LLMSource] = []
        for src in field.sources:
            found = _locate(src.quote, texts, src.email_id)
            if found is None:
                continue
            if found != src.email_id:
                report.repointed.append(name)
            kept.append(LLMSource(email_id=found, quote=src.quote))
        lost_evidence = len(kept) < len(field.sources)
        unsupported_explicit = field.status == "explicit" and not kept
        field.sources = kept
        if lost_evidence or unsupported_explicit:
            report.downgraded.append(name)
            if field.status == "explicit":
                field.status = "inferred"
            field.confidence = min(field.confidence, UNVERIFIED_CONFIDENCE_CAP)
    return report
