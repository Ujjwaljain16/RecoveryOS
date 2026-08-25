"""
Policy Engine orchestrator — TRD §3.4.

evaluate() itself is ALSO zero-I/O (it only loops over RULES against
already-hydrated context objects) — the caller does all DB/Redis/HTTP work
BEFORE calling this, exactly like each individual rule. This file has the
same forbidden-import restriction as rules.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.policy_engine.rules import RULES, CandidateContext, PaymentContext, PolicyConfigContext


@dataclass(frozen=True)
class PolicyDecision:
    verdict: str  # ALLOW | BLOCK | ESCALATE
    rule_trace: tuple[dict, ...]  # ordered [{rule, passed, reason}]


def evaluate(
    payment: PaymentContext,
    candidate: CandidateContext,
    policy_config: PolicyConfigContext,
) -> PolicyDecision:
    """
    Runs RULES in order, short-circuiting on the first failure. Every rule
    (passed or not) up to and including the failing one is recorded in
    rule_trace, in order — an ALLOW verdict's trace is the full 7-rule pass;
    a BLOCK/ESCALATE verdict's trace stops at (and includes) the rule that
    failed, so the trace always answers "which rule, and why."
    """
    trace: list[dict] = []
    for rule in RULES:
        result = rule.check(payment, candidate, policy_config)
        trace.append({"rule": rule.name, "passed": result.passed, "reason": result.reason})
        if not result.passed:
            verdict = "ESCALATE" if rule.escalates_on_fail else "BLOCK"
            return PolicyDecision(verdict=verdict, rule_trace=tuple(trace))
    return PolicyDecision(verdict="ALLOW", rule_trace=tuple(trace))
