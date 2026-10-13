"""Score the extractor against the golden set.

    uv run scripts/eval_extraction.py                      # Bedrock, ~$0.10 per run
    uv run scripts/eval_extraction.py --llm oracle         # harness self-test, $0
    uv run scripts/eval_extraction.py --llm anthropic --model claude-haiku-4-5

Exits non-zero unless accuracy >= 90% and every injection case is flagged.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from freight.extraction import evaluation
from freight.extraction.llm.bedrock import client_from_config
from freight.extraction.llm.port import LLMClient

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--llm", choices=["bedrock", "anthropic", "oracle"], default="bedrock")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--golden", type=Path, default=ROOT / "tests/eval/golden.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "tests/eval/results")
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()

    cases = evaluation.load_cases(args.golden)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    llm: LLMClient
    if args.llm == "oracle":
        llm = evaluation.oracle_client(cases)
    else:
        llm = client_from_config(args.llm, args.model, args.region)

    report = evaluation.run(cases, llm)
    summary = {"model": llm.model_id, **report.summary()}
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.llm != "oracle":
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"{evaluation.stamp()}-{args.llm}.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), "utf-8")
        print(f"saved {path.relative_to(ROOT)}", file=sys.stderr)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
