"""The LLM port: the only shape the rest of the system knows about a model provider.

Swapping Bedrock for another provider means writing one adapter that satisfies ``LLMClient``.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0


class LLMResult[T: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True)

    parsed: T
    usage: Usage
    model_id: str
    latency_ms: int
    cached: bool = False


class LLMError(RuntimeError):
    """The model could not produce schema-valid output (refusal, truncation, outage)."""


class LLMClient(Protocol):
    @property
    def model_id(self) -> str: ...

    def extract[T: BaseModel](self, system: str, prompt: str, schema: type[T]) -> LLMResult[T]: ...
