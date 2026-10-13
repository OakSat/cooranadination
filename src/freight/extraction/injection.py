"""Cheap, deterministic prompt-injection tripwire that runs next to the model's own flag.

Neither signal can change an order's state. A hit only routes the order to a rep (policy.py),
which is the safe outcome for both true and false positives.
"""

import re

from freight.domain.thread import Direction, ThreadMessage

PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
        r"disregard (the |your )?(system|previous) (prompt|instructions)",
        r"ignoriere (alle )?(vorherigen|bisherigen) (anweisungen|regeln)",
        r"ignora (todas )?las instrucciones (anteriores|previas)",
        r"you are now",
        r"system prompt",
        r"(approve|book|confirm) (this|the) (order|shipment|booking) "
        r"(immediately|automatically|now)",
        r"set (the )?price to",
        r"(wire|transfer|send) (the )?(payment|money) to",
        r"\biban\b",
        r"</?email\b",
    )
)


def suspicious_spans(thread: list[ThreadMessage]) -> list[str]:
    hits: list[str] = []
    for m in thread:
        if m.direction is not Direction.INBOUND:
            continue
        for pattern in PATTERNS:
            if match := pattern.search(m.searchable):
                hits.append(match.group(0))
    return hits
