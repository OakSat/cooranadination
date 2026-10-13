"""Deterministic stand-in for the model, used by unit and integration tests.

Responses are chosen by a function of the prompt, so a test can script a whole conversation
("first email -> these fields, reply -> those fields") without any network.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from freight.extraction.llm.port import LLMError, LLMResult, Usage

Responder = Callable[[str], dict[str, Any] | BaseModel]


class FakeLLMClient:
    def __init__(self, responder: Responder, model_id: str = "fake-model") -> None:
        self._responder = responder
        self._model_id = model_id
        self.calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def extract[T: BaseModel](self, system: str, prompt: str, schema: type[T]) -> LLMResult[T]:
        self.calls.append(prompt)
        raw = self._responder(prompt)
        if raw is None:
            raise LLMError("fake responder returned nothing")
        data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
        return LLMResult[T](
            parsed=schema.model_validate(data),
            usage=Usage(input_tokens=len(prompt) // 4, output_tokens=300),
            model_id=self._model_id,
            latency_ms=1,
        )
