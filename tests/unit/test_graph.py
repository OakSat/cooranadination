"""The rendered state machine is well-formed and keeps the approval invariant."""

import json
from collections import deque
from typing import Any

from freight.workflow.graph import START, STATES, End, QuoteFanout, Task, Wait, asl

ARNS = {
    k: f"arn:aws:lambda:us-east-1:123:function:freight-{k}"
    for k in ("extract", "workflow", "quote", "book", "wait")
}


def targets(state: dict[str, Any]) -> list[str]:
    out = [state[k] for k in ("Next", "Default") if k in state]
    out += [c["Next"] for c in state.get("Choices", [])]
    out += [c["Next"] for c in state.get("Catch", [])]
    return out


def test_every_transition_target_exists() -> None:
    definition = asl(ARNS)
    names = set(definition["States"])
    assert definition["StartAt"] in names
    for name, st in definition["States"].items():
        for t in targets(st):
            assert t in names, f"{name} -> {t}"
        for branch in st.get("Branches", []):
            inner = set(branch["States"])
            for s in branch["States"].values():
                assert all(t in inner for t in targets(s))


def test_every_state_is_reachable() -> None:
    definition = asl(ARNS)["States"]
    seen, queue = {START}, deque([START])
    while queue:
        for t in targets(definition[queue.popleft()]):
            if t not in seen:
                seen.add(t)
                queue.append(t)
    assert seen == set(definition)


def test_definition_is_json() -> None:
    assert json.loads(json.dumps(asl(ARNS)))["StartAt"] == START


def graph_edges(skip: tuple[str, str] | None = None) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for name, st in STATES.items():
        out: set[str] = {"Escalate"}
        if isinstance(st, Task):
            out |= set(st.routes.values())
        elif isinstance(st, Wait):
            out |= {t for d, t in st.routes.items() if (name, d) != skip} | {st.on_timeout}
        elif isinstance(st, QuoteFanout):
            out.add(st.next)
        elif isinstance(st, End):
            out = set()
        edges[name] = out
    edges["Escalate"] = {"WaitForHuman", "Failed"}
    return edges


def reachable(edges: dict[str, set[str]]) -> set[str]:
    seen, queue = {"Extract", "WaitForHuman"}, deque(["Extract", "WaitForHuman"])
    while queue:
        for t in edges[queue.popleft()]:
            if t not in seen:
                seen.add(t)
                queue.append(t)
    return seen


def test_booking_only_after_customer_approval() -> None:
    # Remove the one edge that carries the customer's approval: booking becomes unreachable.
    assert "BookMain" in reachable(graph_edges())
    assert "BookMain" not in reachable(graph_edges(skip=("WaitForChoice", "approved")))


def test_waits_pass_the_task_token() -> None:
    for name, st in asl(ARNS)["States"].items():
        if st.get("Resource", "").endswith("waitForTaskToken"):
            assert st["Parameters"]["Payload"]["token.$"] == "$$.Task.Token", name
