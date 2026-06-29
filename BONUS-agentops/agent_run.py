#!/usr/bin/env python3
"""
AgentOps for the Day-23 lab  (deck §14 Harness/Loop/Flywheel + §19 AgentOps Deepdive).

Operate the kind of thing YOU built (Day-3 ReAct e-commerce agent, Day-9 multi-agent):
a multi-step, tool-using agent. This harness runs a small MOCK agent over a few
tasks (zero-key, deterministic), EMITS OTel-GenAI spans to the lab's existing
Collector -> Jaeger, and computes the agent SLIs + failure modes the deck names.

    make up          # Jaeger + OTel Collector already in the stack
    make agentops    # run agent -> spans land in Jaeger, SLIs -> agentops-report.json

Agent observability is NOT request observability: one HTTP 200 can hide a 12-step
loop that burned $5. We measure the trajectory, not the request.

EXTENSION (B3, option c): a fourth failure mode — **hallucinated-tool** (the policy
plans a tool that does not exist) — plus failing spans are marked OTel ERROR so the
Collector's tail-sampling `keep-errors` policy reliably retains agent traces in Jaeger.
See BONUS-agentops/test_agent_run.py for the unit test.

Zero-key by default. --real-llm uses an OpenAI-compatible endpoint (free/local OK).
Span export is best-effort: if the Collector is down, SLIs are still computed.
"""
from __future__ import annotations
import argparse, json, os, sys, time

OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
PRICE_PER_1K = 0.0005  # mock $/1k tokens for cost-per-task

# OTel status types are optional (best-effort tracing); guard the import.
try:
    from opentelemetry.trace import Status, StatusCode
except Exception:  # pragma: no cover - otel not installed
    Status = StatusCode = None


def _set_error(sp, msg):
    """Mark a span ERROR so tail-sampling keep-errors retains the trace."""
    if sp is not None and Status is not None:
        sp.set_status(Status(StatusCode.ERROR, msg))
        sp.set_attribute("agent.failure_mode", msg)


# ---- mock tools (deterministic; echo the Day-3 e-commerce agent) -----------
def tool_search(q):    return {"items": ["SKU-1", "SKU-2"], "tokens": 40}
def tool_get_price(s): return {"price": 19.9, "tokens": 25}
def tool_place_order(s): return {"order_id": "OD-77", "tokens": 30}
def tool_flaky(_):     raise RuntimeError("upstream 503")  # injected tool error
TOOLS = {"search": tool_search, "get_price": tool_get_price,
         "place_order": tool_place_order, "inventory": tool_flaky}

# ---- mock agent "plans" (what a policy/LLM would decide). Each is a trajectory.
TASKS = [
    {"goal": "Mua SKU rẻ nhất", "plan": [("search", "shoes"), ("get_price", "SKU-1"),
                                          ("place_order", "SKU-1")], "expect": True},
    {"goal": "Kiểm tra tồn kho rồi mua", "plan": [("search", "bag"), ("inventory", "SKU-2"),
                                                  ("get_price", "SKU-2"), ("place_order", "SKU-2")],
     "expect": True},  # has a flaky tool -> tool error + retry
    {"goal": "So sánh giá (lỗi vòng lặp)", "plan": [("get_price", "SKU-1")] * 6, "expect": False},  # loop
    {"goal": "Hoàn tiền đơn (tool ảo)", "plan": [("search", "SKU-1"), ("refund", "OD-77")],
     "expect": False},  # EXTENSION: 'refund' is not a real tool -> hallucinated-tool
]
MAX_STEPS = 8


def make_tracer():
    """Best-effort OTel tracer -> OTLP collector. Returns (tracer, provider)."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        prov = TracerProvider(resource=Resource.create({"service.name": "day23-agent"}))
        prov.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=OTLP, insecure=True, timeout=3)))
        trace.set_tracer_provider(prov)
        return trace.get_tracer("agentops"), prov
    except Exception as e:
        print(f"(OTel span export disabled: {e}) — SLIs still computed.", file=sys.stderr)
        return None, None


def detect_loop(actions, window=3):
    """Loop = same (tool,arg) repeated >= window times consecutively."""
    run = 1
    for i in range(1, len(actions)):
        run = run + 1 if actions[i] == actions[i - 1] else 1
        if run >= window:
            return True
    return False


def run_task(task, tracer):
    """Execute one trajectory; return per-task SLI record + emit spans."""
    from contextlib import contextmanager

    @contextmanager
    def span(name, attrs):
        if not tracer:
            yield None
            return
        with tracer.start_as_current_span(name) as sp:
            for k, v in attrs.items():
                sp.set_attribute(k, v)
            yield sp

    steps = tool_calls = tool_errors = hallucinated = tokens = 0
    actions, success = [], False
    with span("invoke_agent", {"gen_ai.operation.name": "invoke_agent",
                               "gen_ai.agent.name": "shopbot", "agent.goal": task["goal"]}) as agent_sp:
        for (tool, arg) in task["plan"]:
            if steps >= MAX_STEPS:
                break
            steps += 1
            actions.append((tool, arg))
            with span("execute_tool", {"gen_ai.operation.name": "execute_tool",
                                       "gen_ai.tool.name": tool}) as tool_sp:
                tool_calls += 1
                if tool not in TOOLS:
                    # EXTENSION: the policy hallucinated a tool that does not exist.
                    hallucinated += 1
                    tokens += 10  # the bad call still cost a round-trip of tokens
                    _set_error(tool_sp, "hallucinated-tool")
                    continue
                try:
                    out = TOOLS[tool](arg)
                    tokens += out.get("tokens", 20)
                    if tool == "place_order":
                        success = True
                except Exception as e:
                    tool_errors += 1
                    tokens += 15  # the failed attempt still cost tokens
                    if tool_sp is not None:
                        tool_sp.record_exception(e)
                    _set_error(tool_sp, "tool-error")
            if detect_loop(actions):
                break  # agent caught in a loop -> abort (no-progress)
        looped = detect_loop(actions)
        if not success or looped:
            _set_error(agent_sp, "task-failed")

    failure_modes = []
    if looped:
        failure_modes.append("loop/no-progress")
    if hallucinated:
        failure_modes.append("hallucinated-tool")
    if tool_errors:
        failure_modes.append("tool-error")
    if not success:
        failure_modes.append("task-failed")
    return {
        "goal": task["goal"], "steps": steps, "tool_calls": tool_calls,
        "tool_errors": tool_errors, "hallucinated_tools": hallucinated, "tokens": tokens,
        "cost_usd": round(tokens / 1000 * PRICE_PER_1K, 6),
        "success": success, "looped": looped,
        "failure_modes": failure_modes,
    }


def aggregate(tasks):
    """Compute the agent SLIs (deck §13/§14) from per-task records."""
    n = len(tasks)
    return {
        "tasks": n,
        "success_rate": round(sum(t["success"] for t in tasks) / n, 3),
        "avg_steps_per_task": round(sum(t["steps"] for t in tasks) / n, 2),
        "tool_error_rate": round(sum(t["tool_errors"] for t in tasks) /
                                 max(sum(t["tool_calls"] for t in tasks), 1), 3),
        "hallucinated_tool_calls": sum(t["hallucinated_tools"] for t in tasks),
        "cost_per_task_usd": round(sum(t["cost_usd"] for t in tasks) / n, 6),
        "loops_detected": sum(t["looped"] for t in tasks),
    }


def main():
    ap = argparse.ArgumentParser(description="AgentOps harness (deck §14/§19)")
    ap.add_argument("--out", default="agentops-report.json")
    ap.add_argument("--real-llm", action="store_true", help="(stub) use OPENAI_API_KEY policy instead of mock plans")
    args = ap.parse_args()
    if args.real_llm and not os.environ.get("OPENAI_API_KEY"):
        print("--real-llm needs OPENAI_API_KEY (free/local OK); falling back to mock.", file=sys.stderr)

    tracer, prov = make_tracer()
    tasks = [run_task(t, tracer) for t in TASKS]
    if prov:
        prov.force_flush(); prov.shutdown()

    agg = aggregate(tasks)
    report = {"generated_at": time.strftime("%H:%M:%SZ", time.gmtime()),
              "span_export": bool(prov), "agent_slis": agg, "per_task": tasks}
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n=== AgentOps report ({agg['tasks']} tasks) ===")
    for k, v in agg.items():
        print(f"  {k:24} {v}")
    print("\n  per-task failure modes:")
    for t in tasks:
        print(f"    - {t['goal'][:30]:32} success={t['success']!s:5} modes={t['failure_modes']}")
    if prov:
        print("\n  Spans exported to Jaeger -> open http://localhost:16686 (service: day23-agent)")
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
