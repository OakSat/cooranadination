"""Claude on Amazon Bedrock, authenticated with the Lambda's IAM role (no API keys).

Two Bedrock surfaces exist. The Mantle endpoint takes ``anthropic.``-prefixed model IDs; the
legacy ``bedrock-runtime`` path takes inference-profile IDs such as
``us.anthropic.claude-haiku-4-5-20251001-v1:0``. The adapter picks the client from the ID, so
switching is a config change (see DEMO_SETUP_GUIDE.md, B1 step 7).
"""

import time

import anthropic
from pydantic import BaseModel

from freight.extraction.llm.port import LLMError, LLMResult, Usage

MAX_OUTPUT_TOKENS = 4096  # cost guard: an extraction is ~1k tokens of JSON


def _is_legacy_profile(model_id: str) -> bool:
    return model_id.startswith(("us.", "eu.", "apac.", "global.")) or model_id.endswith(":0")


class AnthropicMessagesClient:
    """Structured extraction over any Anthropic SDK client that has ``messages.parse``."""

    def __init__(
        self,
        client: anthropic.Anthropic | anthropic.AnthropicBedrock | anthropic.AnthropicBedrockMantle,
        model_id: str,
    ) -> None:
        self._client = client
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def extract[T: BaseModel](self, system: str, prompt: str, schema: type[T]) -> LLMResult[T]:
        started = time.monotonic()
        try:
            response = self._client.messages.parse(
                model=self._model_id,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
        except anthropic.APIConnectionError as e:
            raise LLMError(f"bedrock unreachable: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"bedrock error {e.status_code}: {e.message}") from e
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.stop_reason in ("max_tokens", "refusal") or response.parsed_output is None:
            raise LLMError(f"no structured output (stop_reason={response.stop_reason})")
        return LLMResult[T](
            parsed=response.parsed_output,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            model_id=self._model_id,
            latency_ms=latency_ms,
        )


class BedrockAnthropicClient(AnthropicMessagesClient):
    def __init__(self, model_id: str, region: str = "us-east-1") -> None:
        client: anthropic.AnthropicBedrock | anthropic.AnthropicBedrockMantle
        if _is_legacy_profile(model_id):
            client = anthropic.AnthropicBedrock(aws_region=region)
        else:
            client = anthropic.AnthropicBedrockMantle(aws_region=region)
        super().__init__(client, model_id)


class AnthropicDirectClient(AnthropicMessagesClient):
    """First-party Claude API: swapping providers is one adapter, with no domain change."""

    def __init__(self, model_id: str = "claude-haiku-4-5") -> None:
        super().__init__(anthropic.Anthropic(), model_id)


def client_from_config(provider: str, model_id: str, region: str) -> AnthropicMessagesClient:
    if provider == "anthropic":
        return AnthropicDirectClient(model_id)
    return BedrockAnthropicClient(model_id, region)
