"""Field-level merge of a fresh extraction into the previous version.

The rules, in order, for each field:
1. Nothing new (new is missing): keep what we had. A known value is never forgotten, which is
   what stops us from asking the customer twice.
2. Nothing old: take the new value.
3. Same value: keep it, combine the evidence.
4. Different values: accept the new one only if it comes from the latest customer email and
   either we asked about that field or the old value was only inferred. Otherwise it is a
   conflict, and conflicts on critical fields are asked back (rules.py).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from freight.domain.models import Extracted, FieldStatus, ShipmentRequest

_RANK = {FieldStatus.EXPLICIT: 2, FieldStatus.INFERRED: 1}


class FieldChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    before: FieldStatus
    after: FieldStatus
    reason: str


def _from_email(f: Extracted[Any], email_id: str) -> bool:
    return any(s.email_id == email_id for s in f.sources)


def _union_sources(a: Extracted[Any], b: Extracted[Any]) -> list[Any]:
    seen = {(s.email_id, s.quote) for s in a.sources}
    return [*a.sources, *(s for s in b.sources if (s.email_id, s.quote) not in seen)]


def _conflict(old: Extracted[Any], new: Extracted[Any]) -> Extracted[Any]:
    candidates = list(old.candidates or [old.value])
    for c in new.candidates or [new.value]:
        if c is not None and c not in candidates:
            candidates.append(c)
    return Extracted[Any](
        status=FieldStatus.CONFLICTING,
        confidence=min(old.confidence, new.confidence),
        sources=_union_sources(old, new),
        candidates=candidates,
    )


def merge_field(
    name: str, old: Extracted[Any], new: Extracted[Any], asked: set[str], latest_inbound: str
) -> tuple[Extracted[Any], str]:
    if new.status is FieldStatus.MISSING:
        return old, "kept"
    if old.status is FieldStatus.MISSING:
        return new, "filled"

    answers_us = _from_email(new, latest_inbound) and (
        name in asked or old.status is FieldStatus.INFERRED
    )

    if old.status is FieldStatus.CONFLICTING:
        if new.known and answers_us:
            return new, "resolved"
        return (_conflict(old, new), "still_conflicting") if new.known else (new, "conflicting")

    if new.status is FieldStatus.CONFLICTING:
        return _conflict(old, new), "conflicting"

    if old.value == new.value:
        best = new if _RANK[new.status] > _RANK[old.status] else old
        merged = best.model_copy(
            update={
                "sources": _union_sources(old, new),
                "confidence": max(old.confidence, new.confidence),
            }
        )
        return merged, "confirmed"

    if answers_us or (old.status is FieldStatus.INFERRED and new.status is FieldStatus.EXPLICIT):
        return new, "corrected"
    return _conflict(old, new), "conflicting"


def merge(
    previous: ShipmentRequest | None,
    fresh: ShipmentRequest,
    asked: set[str],
    latest_inbound: str,
) -> tuple[ShipmentRequest, list[FieldChange]]:
    if previous is None:
        return fresh, [
            FieldChange(field=n, before=FieldStatus.MISSING, after=f.status, reason="first")
            for n, f in fresh.fields().items()
            if f.status is not FieldStatus.MISSING
        ]
    merged: dict[str, Extracted[Any]] = {}
    changes: list[FieldChange] = []
    old_fields, new_fields = previous.fields(), fresh.fields()
    for name in old_fields:
        result, reason = merge_field(
            name, old_fields[name], new_fields[name], asked, latest_inbound
        )
        merged[name] = result
        if reason not in ("kept", "confirmed") or result.status is not old_fields[name].status:
            changes.append(
                FieldChange(
                    field=name, before=old_fields[name].status, after=result.status, reason=reason
                )
            )
    request = ShipmentRequest.model_validate(
        {k: v.model_dump(mode="json") for k, v in merged.items()}
    )
    return request, changes
