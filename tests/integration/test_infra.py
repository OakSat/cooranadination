"""Synthesize the CDK app (no AWS calls) and check it against the code's assumptions."""

import json
import os
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from freight.persistence import tables

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "infra" / "cdk.out"

pytest.importorskip("aws_cdk")


@cache
def synth() -> dict[str, dict[str, Any]]:
    env = {
        **os.environ,
        "JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION": "1",
        "CDK_OUTDIR": str(OUT),
    }
    subprocess.run(
        ["uv", "run", "--group", "infra", "python", "infra/app.py"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    return {
        p.name.removesuffix(".template.json"): json.loads(p.read_text())
        for p in OUT.glob("Freight*.template.json")
    }


def resources(template: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [r["Properties"] for r in template["Resources"].values() if r["Type"] == kind]


def test_four_stacks() -> None:
    assert set(synth()) == {
        "FreightStorage",
        "FreightApi",
        "FreightWorkflow",
        "FreightObservability",
    }


def test_tables_match_code() -> None:
    by_name = {
        t["TableName"]: t for t in resources(synth()["FreightStorage"], "AWS::DynamoDB::Table")
    }
    for name, spec in (("freight", tables.FREIGHT), ("freight-cache", tables.CACHE)):
        cfn = by_name[name]
        assert cfn["KeySchema"] == spec["KeySchema"]
        assert sorted(g["IndexName"] for g in cfn.get("GlobalSecondaryIndexes", [])) == sorted(
            g["IndexName"] for g in spec.get("GlobalSecondaryIndexes", [])
        )
    assert by_name["freight-cache"]["TimeToLiveSpecification"]["AttributeName"] == "ttl"
    assert by_name["freight"]["StreamSpecification"]["StreamViewType"] == "NEW_IMAGE"


def test_no_unresolved_nag_errors() -> None:
    synth()
    rows = [
        line
        for report in OUT.glob("AwsSolutions-*-NagReport.csv")
        for line in report.read_text().splitlines()
        if '"Non-Compliant"' in line
    ]
    assert rows == []


def test_lambdas_are_arm64_with_short_log_retention() -> None:
    templates = synth()
    functions = [
        f
        for t in templates.values()
        for f in resources(t, "AWS::Lambda::Function")
        if str(f.get("FunctionName", "")).startswith("freight-")
    ]
    assert len(functions) == 11
    assert all(f["Architectures"] == ["arm64"] for f in functions)
    groups = [g for t in templates.values() for g in resources(t, "AWS::Logs::LogGroup")]
    assert all(g["RetentionInDays"] == 7 for g in groups)


def test_only_extract_may_call_bedrock_and_it_is_capped() -> None:
    wf = synth()["FreightWorkflow"]
    policies = json.dumps(resources(wf, "AWS::IAM::Policy"))
    assert policies.count('bedrock:InvokeModel"') == 1
    extract = next(
        f for f in resources(wf, "AWS::Lambda::Function") if f["FunctionName"] == "freight-extract"
    )
    assert extract["ReservedConcurrentExecutions"] == 5
