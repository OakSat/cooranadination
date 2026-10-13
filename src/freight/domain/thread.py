"""The email thread as the extractor sees it: one entry per stored email, oldest first.

``text`` is only the *new* part of each email (quoted history stripped) so a value quoted back
to us in a reply is not mistaken for a fresh answer. Attachment text is kept separately because
it is untrusted in exactly the same way as the body.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from freight.domain.models import Strict


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ThreadMessage(Strict):
    email_id: str
    direction: Direction
    sent_at: datetime
    subject: str = ""
    text: str
    attachments_text: list[str] = Field(default_factory=list)

    @property
    def searchable(self) -> str:
        """Everything a provenance quote may legitimately point at."""
        return "\n".join([self.subject, self.text, *self.attachments_text])
