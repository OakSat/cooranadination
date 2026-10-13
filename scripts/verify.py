"""Is the plan done? Runs each phase's "done when" check from PLAN.md §4 and the end-to-end
verification from §8, and prints one table.

    uv run scripts/verify.py              # everything that runs locally (no AWS, $0, ~1 min)
    uv run scripts/verify.py --live       # + the golden-set eval on Bedrock (~$0.10)
    uv run scripts/verify.py --deployed   # + checks against the deployed stack (a few cents)
    uv run scripts/verify.py --phase 2 --phase 4      # only some phases
    uv run scripts/verify.py -v           # show output of failed checks
    uv run scripts/verify.py --tick "dashboard login"   # record a manual step as done

Status per phase: DONE (every check passed), LOCAL OK (local checks passed; the AWS or manual
part has not been run), FAIL. Exit code is non-zero only on FAIL.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TICKS = ROOT / "verify-manual.txt"  # manual steps someone has done, one per line, with a date


class Kind(StrEnum):
    LOCAL = "local"
    LIVE = "live"  # calls Bedrock
    DEPLOYED = "deployed"  # needs the deployed stack
    MANUAL = "manual"  # a person has to do it


class Result(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "not run"
    MANUAL = "manual"


@dataclass
class Check:
    name: str
    kind: Kind
    cmd: tuple[str, ...] = ()
    how: str = ""  # for manual checks: what to do


@dataclass
class Phase:
    number: int
    title: str
    done_when: str
    checks: list[Check]
    results: list[tuple[Check, Result, float, str]] = field(default_factory=list)

    @property
    def status(self) -> str:
        got = [r for _, r, _, _ in self.results]
        if Result.FAIL in got:
            return "FAIL"
        if all(r is Result.PASS for r in got):
            return "DONE"
        return "LOCAL OK"


def pytest(*targets: str) -> tuple[str, ...]:
    return (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets)


def script(name: str, *args: str) -> tuple[str, ...]:
    return (PY, f"scripts/{name}", *args)


UNIT = "tests/unit/"
INTEG = "tests/integration/"
FLOW = INTEG + "test_order_flow.py"

PHASES = [
    Phase(
        0,
        "Account & guardrails",
        "bootstrap_check confirms Bedrock invoke, verified SES identities, and the budget",
        [
            Check(
                "bootstrap_check.py --phase0",
                Kind.DEPLOYED,
                script("bootstrap_check.py", "--phase0"),
            ),
        ],
    ),
    Phase(
        1,
        "Deterministic domain core",
        "unit and property tests pass, incl. the 40-pallet and 18-pallet container examples",
        [
            Check(
                "domain + planning tests",
                Kind.LOCAL,
                pytest(
                    *(
                        UNIT + f"test_{m}.py"
                        for m in (
                            "models",
                            "state",
                            "rules",
                            "cargo",
                            "ocean",
                            "air",
                            "alternatives",
                        )
                    )
                ),
            ),
        ],
    ),
    Phase(
        2,
        "Extraction",
        ">= 90% field accuracy on the golden set, and the injection email is flagged",
        [
            Check("extraction unit tests", Kind.LOCAL, pytest(UNIT + "test_extraction.py")),
            Check(
                "eval harness self-test (oracle)",
                Kind.LOCAL,
                script("eval_extraction.py", "--llm", "oracle"),
            ),
            Check(
                "golden set on Bedrock >= 90%, injection flagged",
                Kind.LIVE,
                script("eval_extraction.py"),
            ),
        ],
    ),
    Phase(
        3,
        "Email layer",
        "threading fixture tests pass; no question for a field answered earlier in the thread",
        [
            Check("parse / threading / compose tests", Kind.LOCAL, pytest(UNIT + "test_email.py")),
            Check(
                "never re-asks (end-to-end thread)",
                Kind.LOCAL,
                pytest(
                    FLOW + "::test_demo_happy_path", FLOW + "::test_reply_threads_by_in_reply_to"
                ),
            ),
        ],
    ),
    Phase(
        4,
        "Persistence & idempotency",
        "moto integration tests pass, incl. two workers racing on one transition",
        [Check("repository tests (moto)", Kind.LOCAL, pytest(INTEG + "test_repo.py"))],
    ),
    Phase(
        5,
        "Orchestration (CDK)",
        "an .eml uploaded to S3 reaches AWAITING_CUSTOMER_INFO and a sandbox email arrives",
        [
            Check("state machine graph is well-formed", Kind.LOCAL, pytest(UNIT + "test_graph.py")),
            Check("cdk synth + cdk-nag clean", Kind.LOCAL, pytest(INTEG + "test_infra.py")),
            Check(
                "handlers (API GW / SQS / EventBridge events)",
                Kind.LOCAL,
                pytest(INTEG + "test_handlers.py"),
            ),
            Check(
                "deployed demo (covers the S3 -> questions email path)",
                Kind.DEPLOYED,
                script("demo_e2e.py", "--non-interactive"),
            ),
        ],
    ),
    Phase(
        6,
        "Providers & caching",
        "quotes return 3 ranked options; killing one provider still yields options + an alarm",
        [
            Check(
                "provider, retry, breaker, idempotent booking tests",
                Kind.LOCAL,
                pytest(UNIT + "test_providers.py"),
            ),
            Check(
                "dead provider degrades in the full flow",
                Kind.LOCAL,
                pytest(FLOW + "::test_dead_provider_still_quotes"),
            ),
            Check("deployed race + price change", Kind.DEPLOYED, script("chaos_race.py")),
            Check(
                "alarm on a dead provider",
                Kind.MANUAL,
                how="set /freight/demo/chaos/provider-down=air-carrier, run 2 demo orders, "
                "see freight-provider-circuit-open go to ALARM, then set it back to none",
            ),
        ],
    ),
    Phase(
        7,
        "HITL & customer approval",
        "a sensitive order pauses for review, an approve sends the quote, a link click books",
        [
            Check(
                "full order flows (review, approve, choose, book, reject, injection)",
                Kind.LOCAL,
                pytest(FLOW),
            ),
            Check(
                "signed links + policy tests",
                Kind.LOCAL,
                pytest(UNIT + "test_policy_and_security.py"),
            ),
            Check(
                "deployed demo end to end",
                Kind.DEPLOYED,
                script("demo_e2e.py", "--non-interactive"),
            ),
            Check(
                "dashboard login and approve by hand",
                Kind.MANUAL,
                how="DEMO_SETUP_GUIDE.md E2 steps 4-5",
            ),
        ],
    ),
    Phase(
        8,
        "Observability & hardening",
        "metrics, dashboard, alarms, log redaction, IAM review (cdk-nag), injection test",
        [
            Check("ruff lint", Kind.LOCAL, (PY, "-m", "ruff", "check", ".")),
            Check("ruff format", Kind.LOCAL, (PY, "-m", "ruff", "format", "--check", ".")),
            Check("mypy --strict (src)", Kind.LOCAL, (PY, "-m", "mypy")),
            Check(
                "mypy --strict (scripts, infra)",
                Kind.LOCAL,
                (
                    PY,
                    "-m",
                    "mypy",
                    "--strict",
                    "--ignore-missing-imports",
                    "--explicit-package-bases",
                    "infra/app.py",
                    "infra/build.py",
                    "infra/stacks",
                ),
            ),
            Check(
                "cdk-nag: no unresolved findings",
                Kind.LOCAL,
                pytest(INTEG + "test_infra.py::test_no_unresolved_nag_errors"),
            ),
            Check(
                "injection + redaction tests",
                Kind.LOCAL,
                pytest(
                    FLOW + "::test_unknown_sender_injection_goes_to_review",
                    UNIT + "test_policy_and_security.py::test_redaction_masks_pii_but_not_dates",
                ),
            ),
        ],
    ),
    Phase(
        9,
        "Deployment & demo",
        "DEMO_SETUP_GUIDE.md verified by a fresh deploy that follows it word for word",
        [
            Check("guide names every script and stack", Kind.LOCAL, ("internal:guide",)),
            Check(
                "readiness check on the deployed stack", Kind.DEPLOYED, script("bootstrap_check.py")
            ),
            Check(
                "fresh deploy following the guide word for word",
                Kind.MANUAL,
                how="DEMO_SETUP_GUIDE.md Parts D-G in a clean account/region, then teardown",
            ),
        ],
    ),
    Phase(
        10,
        "Write-up",
        "docs/architecture.md answers all 14 topics and the follow-up, linking to the code",
        [
            Check(
                "architecture.md covers every topic, links resolve",
                Kind.LOCAL,
                ("internal:architecture",),
            )
        ],
    ),
]


# --- internal checks ---------------------------------------------------------------------------


def check_guide() -> tuple[bool, str]:
    text = (ROOT / "DEMO_SETUP_GUIDE.md").read_text("utf-8")
    needed = [
        "bootstrap_check.py",
        "seed.py",
        "send_inbound.py",
        "demo_e2e.py",
        "chaos_race.py",
        "report_kpis.py",
        "teardown.sh",
        "verify.py",
        "FreightStorage",
        "FreightApi",
        "FreightWorkflow",
        "FreightObservability",
    ]
    missing = [n for n in needed if n not in text]
    stale = [s for s in ("FreightDashboard", "Docker Desktop") if s in text]
    ok = not missing and not stale
    return ok, f"missing: {missing}; stale: {stale}" if not ok else ""


def check_architecture() -> tuple[bool, str]:
    path = ROOT / "docs" / "architecture.md"
    if not path.exists():
        return False, "docs/architecture.md does not exist"
    text = path.read_text("utf-8")
    missing = [
        f"Topic {n}" for n in range(1, 15) if not re.search(rf"^#+ .*Topic {n}\b", text, re.M)
    ]
    if not re.search(r"^#+ .*[Ff]ollow-up", text, re.M):
        missing.append("follow-up")
    broken = [
        link
        for link in re.findall(r"\]\(((?:\.\./)?(?:src|infra|scripts|tests)/[^)#]+)", text)
        if not (path.parent / link).resolve().exists()
    ]
    ok = not missing and not broken
    return ok, f"missing sections: {missing}; broken links: {broken}" if not ok else ""


INTERNAL = {"internal:guide": check_guide, "internal:architecture": check_architecture}


def ticked() -> dict[str, str]:
    if not TICKS.exists():
        return {}
    out = {}
    for line in TICKS.read_text("utf-8").splitlines():
        if "\t" in line:
            day, name = line.split("\t", 1)
            out[name] = day
    return out


def tick(fragment: str) -> int:
    manual = [c.name for ph in PHASES for c in ph.checks if c.kind is Kind.MANUAL]
    matches = [n for n in manual if fragment.lower() in n.lower()]
    if len(matches) != 1:
        print(f"'{fragment}' matches {matches or 'nothing'}; manual steps are: {manual}")
        return 2
    with TICKS.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d')}\t{matches[0]}\n")
    print(f"recorded: {matches[0]}")
    return 0


@cache
def run(cmd: tuple[str, ...]) -> tuple[bool, str, float]:
    """Run once per distinct command (demo_e2e backs several phases)."""
    started = time.monotonic()
    if cmd and cmd[0] in INTERNAL:
        ok, out = INTERNAL[cmd[0]]()
        return ok, out, time.monotonic() - started
    env = {**os.environ, "JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION": "1"}
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    return proc.returncode == 0, proc.stdout + proc.stderr, time.monotonic() - started


def aws_ready() -> bool:
    try:
        import boto3

        boto3.client("sts", region_name="us-east-1").get_caller_identity()
    except Exception:
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--live", action="store_true", help="run the Bedrock eval (~$0.10)")
    p.add_argument("--deployed", action="store_true", help="run checks on the deployed stack")
    p.add_argument("--phase", type=int, action="append", help="only these phases")
    p.add_argument("-v", "--verbose", action="store_true", help="print output of failures")
    p.add_argument("--tick", metavar="NAME", help="record a manual step as done")
    args = p.parse_args()
    if args.tick:
        return tick(args.tick)
    done_by_hand = ticked()

    if (args.live or args.deployed) and not aws_ready():
        print(
            "--live/--deployed need AWS credentials: aws sso login --profile freight; "
            "export AWS_PROFILE=freight"
        )
        return 2

    phases = [ph for ph in PHASES if not args.phase or ph.number in args.phase]
    for ph in phases:
        print(f"Phase {ph.number}: {ph.title} ...", end="", flush=True)
        for check in ph.checks:
            enabled = (
                check.kind is Kind.LOCAL
                or (check.kind is Kind.LIVE and args.live)
                or (check.kind is Kind.DEPLOYED and args.deployed)
            )
            if check.kind is Kind.MANUAL and check.name in done_by_hand:
                ph.results.append((check, Result.PASS, 0.0, f"done {done_by_hand[check.name]}"))
            elif check.kind is Kind.MANUAL:
                ph.results.append((check, Result.MANUAL, 0.0, check.how))
            elif not enabled:
                ph.results.append((check, Result.SKIP, 0.0, f"needs --{check.kind.value}"))
            else:
                ok, out, secs = run(check.cmd)
                ph.results.append((check, Result.PASS if ok else Result.FAIL, secs, out))
        print(f" {ph.status}")

    print()
    width = max(len(c.name) for ph in phases for c in ph.checks)
    for ph in phases:
        print(f"Phase {ph.number}  {ph.title}  [{ph.status}]")
        print(f"   done when: {ph.done_when}")
        for check, result, secs, out in ph.results:
            mark = {"PASS": "✔", "FAIL": "✘", "not run": "•", "manual": "✋"}
            timed = result in (Result.PASS, Result.FAIL) and check.kind is not Kind.MANUAL
            note = f"{secs:5.1f}s" if timed else out
            print(f"   {mark[result.value]} {check.name:<{width}}  {result.value:<7} {note}")
            if result is Result.FAIL and args.verbose:
                tail = "\n".join(out.strip().splitlines()[-25:])
                print("      " + tail.replace("\n", "\n      "))
        print()

    statuses = [ph.status for ph in phases]
    done = statuses.count("DONE")
    print(
        f"{done}/{len(phases)} phases DONE, {statuses.count('LOCAL OK')} LOCAL OK, "
        f"{statuses.count('FAIL')} FAIL."
    )
    if not args.live or not args.deployed:
        print(
            "Not run here: "
            + ", ".join(
                flag
                for flag, on in (
                    ("--live (Bedrock eval)", args.live),
                    ("--deployed (AWS stack)", args.deployed),
                )
                if not on
            )
            + ". The plan is done when every phase is DONE and the manual items are ticked off."
        )
    return 1 if "FAIL" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
