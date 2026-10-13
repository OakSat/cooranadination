"""Per-model token prices in USD per million tokens, used for the LLMCostUSD metric.

Bedrock is partner-priced: check aws.amazon.com/bedrock/pricing for us-east-1 and update these
if they differ. Unknown models fall back to the most expensive entry so cost is never
under-reported.
"""

from freight.extraction.llm.port import Usage

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    # Test doubles (FakeLLMClient, the eval oracle) cost nothing.
    "fake": (0.0, 0.0),
    "oracle": (0.0, 0.0),
}
_FALLBACK = max(PRICES_PER_MTOK.values())  # unknown model: never under-report


def _family(model_id: str) -> str:
    for family in PRICES_PER_MTOK:
        if family in model_id:
            return family
    return ""


def cost_usd(model_id: str, usage: Usage) -> float:
    price_in, price_out = PRICES_PER_MTOK.get(_family(model_id), _FALLBACK)
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
