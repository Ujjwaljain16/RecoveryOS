"""
Prometheus metrics — TRD §10, the exact 9-series list (8 counters + 1
histogram), no gauges. Module-level objects on prometheus_client's default
global REGISTRY: each process (api, pipeline_orchestrator, execution_worker,
retry_scheduler) that imports this module shares process-local counters,
scraped independently by Prometheus and aggregated across jobs in Grafana
(`sum(rate(...))` over all scrape targets) — the standard multi-process
Prometheus pattern, not a bug that counts look "split" per service.

Every increment site is documented at its call site (services/pipeline/
ledger.py, services/recovery_engine/orchestrator.py, workers/
execution_worker.py, services/risk_engine/anomaly.py,
services/diagnosis_engine/diagnoser.py) — this module only DEFINES the
series, it never decides when to move them.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

recovery_attempts_total = Counter(
    "recovery_attempts_total",
    "Recovery execution attempts, by action type",
    ["action_type"],
)

recovery_success_total = Counter(
    "recovery_success_total",
    "Recovery execution attempts that reached outcome=SUCCESS, by action type",
    ["action_type"],
)

revenue_at_risk_paise_total = Counter(
    "revenue_at_risk_paise_total",
    "Cumulative revenue_at_risk_paise across every terminal recovery_ledger row (paise)",
)

revenue_recovered_paise_total = Counter(
    "revenue_recovered_paise_total",
    "Cumulative actual_recovery_paise across every terminal recovery_ledger row (paise)",
)

# Gauge, not Counter, despite the "_total" name TRD §10 gives it: a single
# row's incremental_recovery_paise (actual - baseline) is genuinely SIGNED
# (recovery_ledger.incremental_recovery_paise itself allows negative --
# RecoveryOS can recover less than the naive baseline would have on any
# given payment). prometheus_client's Counter.inc() raises on a negative
# argument -- a Gauge accumulated via .inc(delta) with either sign is the
# correct running-sum primitive here; the exported series name matches
# TRD's spec exactly, only the underlying metric TYPE differs from a
# literal (but arithmetically impossible) monotonic counter.
incremental_revenue_paise_total = Gauge(
    "incremental_revenue_paise_total",
    "Cumulative incremental_recovery_paise (actual - baseline) across every terminal "
    "recovery_ledger row (paise) -- signed; a Gauge accumulated via inc(delta), not a "
    "monotonic Counter, since any single row's contribution can be negative",
)

policy_blocks_total = Counter(
    "policy_blocks_total",
    "Policy decisions that did not ALLOW, labeled by the specific rule that blocked/escalated",
    ["rule"],
)

stopping_rule_triggers_total = Counter(
    "stopping_rule_triggers_total",
    "Recovery attempts where a stopping rule fired (MAX_RETRIES, STOP_AFTER_SUCCESS), by reason",
    ["reason"],
)

systemic_degradation_events_total = Counter(
    "systemic_degradation_events_total",
    "High-severity, is_anomaly=true anomaly_windows rows persisted for scope_type=bank",
    ["bank"],
)

diagnosis_latency_seconds = Histogram(
    "diagnosis_latency_seconds",
    "Wall-clock time of one full diagnose() call (diagnoser_role read -> "
    "investigation/LLM/fallback), per payment",
)

stream_backlog_depth = Gauge(
    "stream_backlog_depth",
    "Redis Streams consumer-group lag -- entries in the stream never yet delivered to ANY "
    "consumer in the group (XINFO GROUPS' own 'lag' field), by (stream, group). Production "
    "Architecture Domain Audit finding #4: the first ingestion stage (event_processor) had "
    "zero visibility into whether it was falling behind the incoming PAYMENT_FAILED volume -- "
    "this answers 'is RecoveryOS actually processing the stream, or quietly falling behind?' "
    "for every Redis-Streams consumer group in the system, not just a raw throughput count.",
    ["stream", "group"],
)

# ─── Reconciled gauges (Production Architecture Domain Audit finding #1) ───
#
# "Prometheus is observability; Postgres is financial truth." The Counters
# above (revenue_at_risk_paise_total etc.) are real and useful for RATE/
# TREND panels, but they live only in process memory: any of the three
# background workers restarting under `restart: always` (a routine,
# expected event -- a bank API 500, a transient DB error, an OOM) zeroes
# them with zero reconciliation against the durable recovery_ledger table
# that is the actual source of truth. The Control Tower dashboard's own
# panel descriptions claim "no separate internal truth vs customer-facing
# truth" -- these gauges make that literally true for the headline
# business tiles: recomputed via a live SQL aggregate on EVERY /metrics
# scrape (apps/api/routers/health.py), so a worker restart can never
# desync them -- there is no accumulated state to lose, because none is
# kept. Never .inc()'d; only ever .set() from a fresh query.
revenue_at_risk_paise_reconciled = Gauge(
    "revenue_at_risk_paise_reconciled",
    "SUM(revenue_at_risk_paise) over ALL of recovery_ledger, recomputed live on every scrape "
    "-- immune to any worker's in-process counter resetting on restart.",
)

revenue_recovered_paise_reconciled = Gauge(
    "revenue_recovered_paise_reconciled",
    "SUM(actual_recovery_paise) over ALL of recovery_ledger, recomputed live on every scrape.",
)

incremental_revenue_paise_reconciled = Gauge(
    "incremental_revenue_paise_reconciled",
    "SUM(incremental_recovery_paise) over ALL of recovery_ledger, recomputed live on every scrape.",
)

recovery_attempts_reconciled = Gauge(
    "recovery_attempts_reconciled",
    "COUNT(*) of terminal `recoveries` rows by action_type, recomputed live on every scrape -- "
    "the reconciled counterpart to recovery_attempts_total, for the same restart-immunity "
    "reason (the Recovery Rate tile has the identical vulnerability F1 identified for revenue).",
    ["action_type"],
)

recovery_success_reconciled = Gauge(
    "recovery_success_reconciled",
    "COUNT(*) of `recoveries` rows with outcome='SUCCESS' by action_type, recomputed live on "
    "every scrape.",
    ["action_type"],
)

ai_diagnoser_fallback_total = Counter(
    "ai_diagnoser_fallback_total",
    "Diagnoses produced by the deterministic fallback path (is_fallback=true) -- "
    "an honest reliability signal for how often the LLM path degrades",
)

# ─── Phase 11: bounded AI recommendation fusion ─────────────────────────────
ai_recommendation_available_total = Counter(
    "ai_recommendation_available_total",
    "A RecoveryRecommendation was successfully produced and available for a decision cycle "
    "(regardless of whether fusion is enabled or the recommendation was ultimately accepted).",
)
ai_tie_break_applied_total = Counter(
    "ai_tie_break_applied_total",
    "AI's recommended action won an economic near-tie and became the final chosen_action.",
)
ai_tie_break_rejected_total = Counter(
    "ai_tie_break_rejected_total",
    "AI's recommended action did NOT change the outcome, by reason.",
    ["reason"],  # outside_tolerance | tie_break_rejected_policy
)
ai_risk_escalations_total = Counter(
    "ai_risk_escalations_total",
    "AIRiskSignalEscalationRule fired -- a closed-set AI risk_flags signal forced ESCALATE.",
)
ai_outcome_delta_total = Counter(
    "ai_outcome_delta_total",
    "THE headline Phase 11 number: how many decisions ended with a different final action "
    "solely because AI recommendation fusion was enabled, by cause.",
    ["cause"],  # tie_break | risk_escalation
)

# ─── Pre-registration of known label values ────────────────────────────────
# prometheus_client only emits a metric family's HELP/TYPE lines (and thus
# the whole series) once at least one label combination has actually been
# `.labels(...)`'d — a Counter/Histogram with labels that has NEVER been
# incremented is entirely ABSENT from generate_latest()'s output, not
# present-at-zero. Pre-touching every label value this process knows about
# in advance (duplicated here as plain strings, not imported from
# services/*, to keep recoveryos/ -- the shared low-level package -- from
# depending on the service layer that depends on IT) makes every TRD §10
# series show up on /metrics immediately at process start, at 0, rather
# than only after the first real event of each kind -- which is what
# test_metrics_endpoint_exposes_all_required_series() actually needs to be
# able to assert against a freshly-started process. systemic_degradation_
# events_total{bank} pre-registers the actual bank codes this system's own
# simulator/synthetic traffic consistently uses (not an arbitrary guess) --
# a genuinely new bank string would still appear on first real occurrence,
# same as any other label value not pre-registered.
_KNOWN_ACTION_TYPES = (
    "RETRY_NOW",
    "RETRY_LATER",
    "ALT_ROUTE",
    "REMINDER",
    "ESCALATE",
    "DO_NOTHING",
)
_KNOWN_POLICY_RULES = (
    "EligibilityRule",
    "OptOutRule",
    "AIRiskSignalEscalationRule",
    "CooldownRule",
    "RetryLimitRule",
    "AmountLimitRule",
    "EMandateRetryComplianceRule",
    "AutopayExecutionWindowRule",
    "QuietHoursComplianceRule",
    "SystemicSuppressionRule",
    "MinExpectedValueRule",
)
_KNOWN_STOPPING_REASONS = ("MAX_RETRIES", "STOP_AFTER_SUCCESS")
_KNOWN_BANKS = ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YESB")
_KNOWN_TIE_BREAK_REJECT_REASONS = ("outside_tolerance", "tie_break_rejected_policy")
_KNOWN_AI_OUTCOME_DELTA_CAUSES = ("tie_break", "risk_escalation")

for _action in _KNOWN_ACTION_TYPES:
    recovery_attempts_total.labels(action_type=_action)
    recovery_success_total.labels(action_type=_action)
for _rule in _KNOWN_POLICY_RULES:
    policy_blocks_total.labels(rule=_rule)
for _reason in _KNOWN_STOPPING_REASONS:
    stopping_rule_triggers_total.labels(reason=_reason)
for _bank in _KNOWN_BANKS:
    systemic_degradation_events_total.labels(bank=_bank)
for _tie_reason in _KNOWN_TIE_BREAK_REJECT_REASONS:
    ai_tie_break_rejected_total.labels(reason=_tie_reason)
for _cause in _KNOWN_AI_OUTCOME_DELTA_CAUSES:
    ai_outcome_delta_total.labels(cause=_cause)
