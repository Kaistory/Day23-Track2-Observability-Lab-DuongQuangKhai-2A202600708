#!/usr/bin/env python3
"""Unit tests for the AgentOps harness extension (B3, option c).

Run:  python BONUS-agentops/test_agent_run.py     (or: pytest BONUS-agentops/)

Covers loop detection and the new `hallucinated-tool` failure mode. Runs without a
tracer (tracer=None), so it needs no running stack and no OTel install.
"""
from __future__ import annotations

import agent_run as a


def test_detect_loop():
    assert a.detect_loop([("x", 1), ("x", 1), ("x", 1)]) is True       # 3 in a row
    assert a.detect_loop([("x", 1), ("y", 1), ("x", 1)]) is False       # alternating
    assert a.detect_loop([("x", 1), ("x", 1)]) is False                 # only 2 in a row


def test_success_task_has_no_failure_modes():
    rec = a.run_task(a.TASKS[0], tracer=None)   # search -> get_price -> place_order
    assert rec["success"] is True
    assert rec["failure_modes"] == []


def test_tool_error_task():
    rec = a.run_task(a.TASKS[1], tracer=None)   # contains the flaky `inventory` tool
    assert rec["tool_errors"] == 1
    assert "tool-error" in rec["failure_modes"]
    assert rec["success"] is True               # still completes the order afterwards


def test_loop_task():
    rec = a.run_task(a.TASKS[2], tracer=None)   # get_price x6
    assert rec["looped"] is True
    assert rec["success"] is False
    assert "loop/no-progress" in rec["failure_modes"]
    assert rec["steps"] <= a.MAX_STEPS


def test_hallucinated_tool_task():
    rec = a.run_task(a.TASKS[3], tracer=None)   # plans a non-existent `refund` tool
    assert rec["hallucinated_tools"] == 1
    assert "hallucinated-tool" in rec["failure_modes"]
    assert rec["success"] is False


def test_aggregate_slis():
    tasks = [a.run_task(t, tracer=None) for t in a.TASKS]
    agg = a.aggregate(tasks)
    assert agg["tasks"] == 4
    assert agg["success_rate"] == 0.5           # tasks 1 & 2 succeed, 3 & 4 fail
    assert agg["loops_detected"] == 1
    assert agg["hallucinated_tool_calls"] == 1


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
