"""The order state machine, defined once.

``asl()`` renders it as Amazon States Language for CDK, and the local runner
(tests/integration/local_sfn.py) interprets the same definition in-process. So what the
end-to-end test exercises is exactly what gets deployed.

Step results route on ``next``; waits route on the answer's ``decision``.
"""

from dataclasses import dataclass, field
from typing import Any

DAY = 86400


@dataclass(frozen=True)
class Task:
    function: str  # which Lambda: extract | workflow | quote | book
    step: str  # handler step name, see handlers/workflow.py ``dispatch``
    routes: dict[str, str]
    params: dict[str, str] = field(default_factory=dict)  # payload key -> JSONPath
    result_path: str = "$.last"


@dataclass(frozen=True)
class Wait:
    waiting_for: str
    routes: dict[str, str]  # decision -> next state
    timeout_seconds: int | None = None
    on_timeout: str = "Escalate"


@dataclass(frozen=True)
class QuoteFanout:
    providers: tuple[str, ...]
    next: str


@dataclass(frozen=True)
class End:
    succeed: bool = True


State = Task | Wait | QuoteFanout | End

START = "Route"  # a Choice on the execution input's "route": "human" or anything else
ROUND = {"round": "$.plan.body.round"}  # the Plan step's result

STATES: dict[str, State] = {
    "Extract": Task(
        "extract",
        "extract",
        {"ask": "WaitForCustomerInfo", "human": "WaitForHuman", "plan": "Plan"},
    ),
    "WaitForCustomerInfo": Wait(
        "customer_info", {"reply": "Extract"}, timeout_seconds=7 * DAY, on_timeout="Escalate"
    ),
    "Plan": Task(
        "workflow",
        "plan",
        {"quote": "Quote", "human": "WaitForHuman"},
        result_path="$.plan",
    ),
    "Quote": QuoteFanout(("ocean-carrier", "air-carrier", "de-trucking"), next="Rank"),
    "Rank": Task(
        "workflow",
        "rank",
        {"review": "WaitForRep", "customer": "WaitForChoice", "human": "WaitForHuman"},
        params=ROUND,
    ),
    "WaitForRep": Wait("rep_review", {"approve": "SendQuote", "edit": "Plan", "reject": "Cancel"}),
    "SendQuote": Task("workflow", "send_quote", {"customer": "WaitForChoice"}),
    "WaitForChoice": Wait(
        "customer_choice",
        {"approved": "Revalidate", "requote": "Plan", "reply": "Extract"},
        timeout_seconds=7 * DAY,
        on_timeout="Cancel",
    ),
    "Revalidate": Task(
        "book", "revalidate", {"book": "BookMain", "requote": "Plan", "human": "WaitForHuman"}
    ),
    "BookMain": Task(
        "book", "book_main", {"truck": "BookTruck", "human": "WaitForHuman", "done": "Complete"}
    ),
    "BookTruck": Task("book", "book_truck", {"complete": "Complete", "human": "WaitForHuman"}),
    "Complete": Task("workflow", "complete", {"done": "Done"}),
    "WaitForHuman": Wait(
        "human",
        {
            "retry_extraction": "ResumeFromHuman",
            "replan": "ResumeFromHuman",
            "cancel": "ResumeFromHuman",
        },
    ),
    "ResumeFromHuman": Task(
        "workflow",
        "resume_from_human",
        {"extract": "Extract", "plan": "Plan", "done": "Cancelled"},
        params={"action": "$.decision.decision"},
    ),
    "Cancel": Task("workflow", "cancel", {"done": "Cancelled"}),
    "Escalate": Task(
        "workflow",
        "escalate",
        {"human": "WaitForHuman", "fail": "Failed"},
        params={"error": "$.error.Error"},
    ),
    "Done": End(),
    "Cancelled": End(),
    "Failed": End(succeed=False),
}

# Errors worth retrying inside the step before escalating. StaleOrder means another worker
# moved the order; steps re-read state, so a retry converges.
RETRY_ERRORS = [
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
    "ProviderUnavailable",
    "StaleOrder",
]


def _choice_name(state: str) -> str:
    return f"{state}?"


def _task_payload(order_path: str, task: Task) -> dict[str, Any]:
    payload: dict[str, Any] = {"orderId.$": order_path, "step": task.step}
    for k, path in task.params.items():
        payload[f"{k}.$"] = path
    return payload


def _lambda_task(arn: str, payload: dict[str, Any], result_path: str, nxt: str) -> dict[str, Any]:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Parameters": {"FunctionName": arn, "Payload": payload},
        "ResultSelector": {"next.$": "$.Payload.next", "body.$": "$.Payload"},
        "ResultPath": result_path,
        "Retry": [
            {
                "ErrorEquals": RETRY_ERRORS,
                "IntervalSeconds": 2,
                "MaxAttempts": 4,
                "BackoffRate": 2.0,
                "JitterStrategy": "FULL",
            }
        ],
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "Escalate"}],
        "Next": nxt,
    }


def _choice(variable: str, routes: dict[str, str], default: str) -> dict[str, Any]:
    return {
        "Type": "Choice",
        "Choices": [
            {"Variable": variable, "StringEquals": value, "Next": target}
            for value, target in routes.items()
        ],
        "Default": default,
    }


def asl(function_arns: dict[str, str]) -> dict[str, Any]:
    """Render the definition. ``function_arns`` maps extract|workflow|quote|book|wait."""
    states: dict[str, Any] = {
        START: {
            "Type": "Choice",
            "Choices": [
                {
                    "And": [
                        {"Variable": "$.route", "IsPresent": True},
                        {"Variable": "$.route", "StringEquals": "human"},
                    ],
                    "Next": "WaitForHuman",
                }
            ],
            "Default": "Extract",
        }
    }
    order = "$.orderId"
    for name, st in STATES.items():
        if isinstance(st, Task):
            task = _lambda_task(
                function_arns[st.function],
                _task_payload(order, st),
                st.result_path,
                _choice_name(name),
            )
            if name == "Escalate":
                task["Catch"] = [{"ErrorEquals": ["States.ALL"], "Next": "Failed"}]
            states[name] = task
            path = "$.plan.next" if st.result_path == "$.plan" else "$.last.next"
            states[_choice_name(name)] = _choice(path, st.routes, "Escalate")
        elif isinstance(st, Wait):
            wait: dict[str, Any] = {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
                "Parameters": {
                    "FunctionName": function_arns["wait"],
                    "Payload": {
                        "orderId.$": order,
                        "step": "wait",
                        "waitingFor": st.waiting_for,
                        "token.$": "$$.Task.Token",
                    },
                },
                "ResultPath": "$.decision",
                "Catch": [
                    {
                        "ErrorEquals": ["States.Timeout"],
                        "ResultPath": "$.error",
                        "Next": st.on_timeout,
                    },
                    {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "Escalate"},
                ],
                "Next": _choice_name(name),
            }
            if st.timeout_seconds:
                wait["TimeoutSeconds"] = st.timeout_seconds
            states[name] = wait
            states[_choice_name(name)] = _choice("$.decision.decision", st.routes, "Escalate")
        elif isinstance(st, QuoteFanout):
            branches = []
            for provider in st.providers:
                branch_task = _lambda_task(
                    function_arns["quote"],
                    {
                        "orderId.$": order,
                        "step": "quote",
                        "provider": provider,
                        "round.$": "$.plan.body.round",
                    },
                    "$",
                    "unused",
                )
                branch_task.pop("Next")
                branch_task["End"] = True
                branch_task["ResultSelector"] = {"provider": provider, "count.$": "$.Payload.count"}
                # A provider failing must not fail the order: record zero quotes instead.
                branch_task["Catch"] = [
                    {
                        "ErrorEquals": ["States.ALL"],
                        "ResultPath": "$.error",
                        "Next": f"{provider} unavailable",
                    }
                ]
                branches.append(
                    {
                        "StartAt": provider,
                        "States": {
                            provider: branch_task,
                            f"{provider} unavailable": {
                                "Type": "Pass",
                                "Parameters": {"provider": provider, "count": 0},
                                "End": True,
                            },
                        },
                    }
                )
            states[name] = {
                "Type": "Parallel",
                "Branches": branches,
                "ResultPath": "$.quotes",
                "Next": st.next,
            }
        elif isinstance(st, End):
            states[name] = (
                {"Type": "Succeed"}
                if st.succeed
                else {"Type": "Fail", "Error": "OrderFailed", "Cause": "see the order's audit"}
            )
    # A step answered with a route the graph does not know: escalate with a readable error.
    states["UnknownRoute"] = {
        "Type": "Pass",
        "Result": {"Error": "UnknownRoute"},
        "ResultPath": "$.error",
        "Next": "Escalate",
    }
    for st in states.values():
        if st.get("Type") == "Choice" and st.get("Default") == "Escalate":
            st["Default"] = "UnknownRoute"
    return {
        "Comment": "Freight order: one execution per order, named after the order id",
        "StartAt": START,
        "States": states,
    }
