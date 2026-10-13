"""Golden-set scoring for extraction quality (Phase 2 gate: >= 90% field accuracy).

A field is correct when its status *class* matches (known = explicit or inferred, missing,
conflicting) and, for known fields, the value matches on the keys the label specifies. The
exact explicit/inferred split is reported separately in the confusion matrix; it is a judgment
call even for humans, so it does not count against accuracy.
"""

import json
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from freight.domain.models import Address, FieldStatus
from freight.domain.thread import ThreadMessage
from freight.extraction.extractor import ExtractionResult, extract
from freight.extraction.llm.fake import FakeLLMClient
from freight.extraction.llm.port import LLMClient, LLMError
from freight.extraction.normalize import DOMAIN_NAME
from freight.planning import catalogs

NUMERIC_TOLERANCE = 0.02
GATE_ACCURACY = 0.90
LLM_NAME = {v: k for k, v in DOMAIN_NAME.items()}


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def thread_of(case: dict[str, Any]) -> list[ThreadMessage]:
    return [ThreadMessage.model_validate(m) for m in case["thread"]]


def _fold(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _same_city(expected: dict[str, Any], got: dict[str, Any]) -> bool:
    if _fold(expected["city"]) == _fold(got.get("city", "")):
        return True
    a = catalogs.resolve_location(Address(city=expected["city"], country=expected["country"]))
    try:
        b = catalogs.resolve_location(Address(city=got["city"], country=got["country"]))
    except (KeyError, ValueError):
        return False
    return a is not None and b is not None and (a.port, a.airport) == (b.port, b.airport)


def values_match(expected: Any, got: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(got, dict):
            return False
        if "city" in expected:
            rest = {k: v for k, v in expected.items() if k != "city"}
            return _same_city(expected, got) and values_match(rest, got)
        return all(values_match(v, got.get(k)) for k, v in expected.items())
    if isinstance(expected, bool) or expected is None:
        return bool(expected == got)
    if isinstance(expected, (int, float)):
        if not isinstance(got, (int, float)):
            return False
        return abs(got - expected) <= NUMERIC_TOLERANCE * max(abs(expected), 1)
    if isinstance(expected, str):
        return isinstance(got, str) and _fold(expected) == _fold(got)
    return bool(expected == got)


def _status_class(status: str) -> str:
    return "known" if status in ("explicit", "inferred") else status


def field_correct(label: dict[str, Any], got: dict[str, Any]) -> bool:
    if _status_class(label["status"]) != _status_class(got["status"]):
        return False
    if label["status"] == "conflicting":
        got_c = got.get("candidates", [])
        return all(any(values_match(e, g) for g in got_c) for e in label["candidates"])
    if label["status"] == "missing":
        return True
    return values_match(label["value"], got.get("value"))


@dataclass
class CaseResult:
    case_id: str
    correct: int
    total: int
    wrong_fields: list[str]
    injection_expected: bool
    injection_flagged: bool
    cost_usd: float
    latency_ms: int
    error: str | None = None


@dataclass
class Report:
    cases: list[CaseResult] = field(default_factory=list)
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)
    per_field: dict[str, list[bool]] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        total = sum(c.total for c in self.cases)
        return sum(c.correct for c in self.cases) / total if total else 0.0

    @property
    def injection_ok(self) -> bool:
        flagged = [c.injection_flagged for c in self.cases if c.injection_expected]
        return bool(flagged) and all(flagged)

    @property
    def false_injection_flags(self) -> list[str]:
        return [c.case_id for c in self.cases if c.injection_flagged and not c.injection_expected]

    @property
    def passed(self) -> bool:
        return self.accuracy >= GATE_ACCURACY and self.injection_ok

    def summary(self) -> dict[str, Any]:
        latencies = [c.latency_ms for c in self.cases if c.error is None]
        return {
            "accuracy": round(self.accuracy, 4),
            "gate": GATE_ACCURACY,
            "passed": self.passed,
            "injection_flagged": self.injection_ok,
            "false_injection_flags": self.false_injection_flags,
            "cases": len(self.cases),
            "errors": [c.case_id for c in self.cases if c.error],
            "cost_usd": round(sum(c.cost_usd for c in self.cases), 4),
            "latency_ms_p50": int(statistics.median(latencies)) if latencies else 0,
            "latency_ms_max": max(latencies, default=0),
            "per_field_accuracy": {
                k: round(sum(v) / len(v), 3) for k, v in sorted(self.per_field.items())
            },
            "confusion_expected_vs_got": {
                f"{e}->{g}": n for (e, g), n in sorted(self.confusion.items())
            },
            "wrong": {c.case_id: c.wrong_fields for c in self.cases if c.wrong_fields},
        }


def score_case(case: dict[str, Any], result: ExtractionResult, report: Report) -> CaseResult:
    got_fields = result.fresh.model_dump(mode="json")
    wrong: list[str] = []
    for name, label in case["expected"].items():
        got = got_fields[name]
        report.confusion[(label["status"], got["status"])] += 1
        ok = field_correct(label, got)
        report.per_field.setdefault(name, []).append(ok)
        if not ok:
            wrong.append(name)
    total = len(case["expected"])
    return CaseResult(
        case_id=case["id"],
        correct=total - len(wrong),
        total=total,
        wrong_fields=wrong,
        injection_expected=bool(case.get("injection")),
        injection_flagged=result.injection_suspected,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
    )


def run(cases: list[dict[str, Any]], llm: LLMClient) -> Report:
    report = Report()
    for case in cases:
        try:
            result = extract(thread_of(case), llm=llm, today=date.fromisoformat(case["today"]))
        except LLMError as e:
            total = len(case["expected"])
            report.cases.append(
                CaseResult(
                    case["id"], 0, total, list(case["expected"]), False, False, 0.0, 0, str(e)
                )
            )
            continue
        report.cases.append(score_case(case, result, report))
    return report


# --- Oracle: turn labels back into model output, to test the harness itself for free ----------


def _llm_value(domain_name: str, value: Any) -> Any:
    if domain_name == "total_weight_kg":
        return {"amount": value, "unit": "kg"}
    if domain_name == "dimensions":
        return {
            "length": value["length_cm"],
            "width": value["width_cm"],
            "height": value["height_cm"],
            "unit": "cm",
            "pieces": value.get("pieces", 1),
        }
    return value


def oracle_output(case: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, label in case["expected"].items():
        status = label["status"]
        entry: dict[str, Any] = {"status": status, "confidence": 0.0, "sources": []}
        if status in (FieldStatus.EXPLICIT, FieldStatus.INFERRED):
            entry["value"] = _llm_value(name, label["value"])
            entry["confidence"] = 0.95 if status == "explicit" else 0.7
            entry["sources"] = [{"email_id": label["email_id"], "quote": label["quote"]}]
        elif status == FieldStatus.CONFLICTING:
            entry["candidates"] = [_llm_value(name, c) for c in label["candidates"]]
            entry["confidence"] = 0.5
            entry["sources"] = [
                {"email_id": label["email_id"], "quote": q} for q in label["quotes"]
            ]
        out[LLM_NAME.get(name, name)] = entry
    out["language"] = case["lang"]
    out["intent"] = "new_request"
    out["suspected_instructions"] = bool(case.get("injection"))
    return out


def oracle_client(cases: list[dict[str, Any]]) -> FakeLLMClient:
    """Answers each prompt with the labels of the case whose first email it contains."""
    by_marker = {c["thread"][0]["text"][:60]: c for c in cases}

    def respond(prompt: str) -> dict[str, Any]:
        for marker, case in by_marker.items():
            if marker.replace("<", "&lt;") in prompt:
                return oracle_output(case)
        raise LLMError("oracle has no label for this prompt")

    return FakeLLMClient(respond, model_id="oracle")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")
