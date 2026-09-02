"use client";

import { useEffect, useState } from "react";
import { apiGet, ApiError, formatBps, formatPaise } from "@/lib/api";

type CandidateAction = {
  candidate_id: string;
  action_type: string;
  recovery_prob_bps: number;
  expected_value_paise: number;
  cost_paise: number;
  friction_penalty_paise: number;
  risk_penalty_paise: number;
  action_confidence: number | null;
  is_selected: boolean;
};

type PaymentDetail = {
  payment_id: string;
  payment: {
    customer_id: string;
    amount_paise: number;
    method: string;
    bank: string | null;
    status: string;
    failure_code: string | null;
    failure_class: string | null;
    created_at: string;
    failed_at: string | null;
  };
  events: { event_id: string; event_type: string; occurred_at: string }[];
  diagnosis: {
    root_cause: string;
    confidence: number | null;
    confidence_band: string | null;
    is_fallback: boolean;
    model_version: string;
    evidence: { fact: string; source: string }[] | null;
  } | null;
  candidate_actions: CandidateAction[];
  policy_decision: {
    verdict: string;
    rule_trace: { rule: string; passed: boolean; reason: string }[];
    stopping_rule: string | null;
    max_amount_paise: number | null;
  } | null;
  recovery_history: {
    recovery_id: string;
    attempt_number: number;
    action_type: string;
    outcome: string | null;
    recovered_amount_paise: number;
    executed_at: string | null;
  }[];
  ai_fusion: {
    deterministic_chosen_action: string;
    deterministic_chosen_evi_paise: number;
    near_tied_candidates: { action_type: string; evi_paise: number }[];
    tie_tolerance_bps: number;
    ai_recommended_action: string | null;
    ai_confidence: number | null;
    ai_risk_flags: string[];
    tie_break_applied: boolean;
    risk_escalation_applied: boolean;
    final_action: string;
    fusion_reason: string;
  } | null;
};

type MissionEvent = {
  sequence_number: number;
  state: string;
  event_type: string;
  actor: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type MissionResponse = {
  payment_id: string;
  mission: {
    mission_id: string;
    state: string;
    objective: string;
    max_investigation_rounds: number;
    max_attempts: number;
    current_round: number;
    current_attempt: number;
    started_at: string;
    ended_at: string | null;
  };
  events: MissionEvent[];
};

const TERMINAL_MISSION_STATES = new Set(["RECOVERED", "ESCALATED", "TERMINATED"]);
const MISSION_POLL_MS = 3000;

function missionBadgeStyle(state: string): React.CSSProperties {
  if (state === "RECOVERED") return { color: "var(--green)", borderColor: "var(--green)" };
  if (state === "ESCALATED" || state === "TERMINATED") {
    return { color: "var(--amber)", borderColor: "var(--amber)" };
  }
  return { color: "var(--accent)", borderColor: "var(--accent)" };
}

const ACTOR_LABEL: Record<string, string> = {
  system: "SYSTEM",
  ai: "AI",
  policy_engine: "POLICY",
  execution_worker: "EXECUTION",
};

function eventSummary(e: MissionEvent): string {
  const p = e.payload || {};
  switch (e.event_type) {
    case "MISSION_CREATED":
      return "Recovery mission created";
    case "REINVESTIGATION_STARTED":
      return "Previous attempt failed — reinvestigating with new evidence";
    case "HYPOTHESIS_UPDATED":
      return `Root cause: ${p.root_cause ?? "—"}${
        typeof p.confidence === "number" ? ` (${(p.confidence * 100).toFixed(0)}% confidence)` : ""
      }`;
    case "AI_RECOMMENDATION":
      return `Recommends ${p.recommended_action ?? "—"}${
        typeof p.confidence === "number" ? ` (${(p.confidence * 100).toFixed(0)}% confidence)` : ""
      }`;
    case "POLICY_AUTHORIZED":
      return `Authorized: ${p.chosen_action ?? "—"}`;
    case "POLICY_BLOCKED":
      return `Blocked by policy (${p.blocking_rule ?? "—"})`;
    case "POLICY_ESCALATED":
      return `Escalated by policy (${p.blocking_rule ?? "AI risk signal"})`;
    case "POLICY_DO_NOTHING":
      return "Deliberate non-intervention (DO_NOTHING)";
    case "RETRY_LATER_SCHEDULED":
      return "Deferred — a re-evaluation is scheduled";
    case "RECOVERY_FAILED":
      return `Attempt failed${p.action_type ? ` (${p.action_type})` : ""}`;
    case "RECOVERY_SUCCEEDED":
      return `Attempt succeeded${
        typeof p.recovered_amount_paise === "number"
          ? ` — ${formatPaise(p.recovered_amount_paise as number)} recovered`
          : ""
      }`;
    case "OUTCOME_PENDING":
      return "Order created — awaiting payment confirmation";
    case "EXTERNAL_RESOLUTION":
      return `The world changed: a real ${p.outcome ?? ""} webhook arrived`;
    case "MISSION_RECOVERED":
      return "Mission complete — payment recovered";
    case "MISSION_ESCALATED":
      return "Mission complete — handed off to a human";
    case "MISSION_BUDGET_EXHAUSTED":
      return `Mission budget exhausted (${p.reason ?? "—"})`;
    case "STOPPING_RULE_TRIGGERED":
      return `Deterministic stopping rule triggered (${p.stopping_rule ?? "—"})`;
    default:
      return e.event_type.replaceAll("_", " ").toLowerCase();
  }
}

export default function PaymentDetailPage({ params }: { params: { id: string } }) {
  const [detail, setDetail] = useState<PaymentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mission, setMission] = useState<MissionResponse | null>(null);
  const [missionChecked, setMissionChecked] = useState(false);

  // Payment detail (status, recovery_history, diagnosis, policy, ai_fusion)
  // and the Recovery Mission timeline are fetched together, in lockstep,
  // every mission poll tick -- not just once on mount. Recovery History and
  // payment status only ever change as a direct consequence of a mission
  // event landing (a new attempt, an external resolution), so refetching
  // detail separately-and-once left those sections showing stale data
  // (e.g. "failed" / "PENDING") for as long as a judge stayed on the page
  // after a mission had already resolved via reconciliation. Mission
  // polling itself stays 404-tolerant (a real, expected "no mission exists
  // for this payment" case, not an error) and stops once terminal, so a
  // just-triggered scenario's flow visibly grows live -- every poll
  // reflects a real, already-persisted mission_events row, never a
  // synthesized "AI thinking…" animation.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const fetchDetail = () => {
      apiGet<PaymentDetail>(`v1/payments/${params.id}/detail`)
        .then(({ data }) => {
          if (!cancelled) setDetail(data);
        })
        .catch((err) => {
          if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load payment.");
        });
    };

    const fetchMission = () => {
      apiGet<MissionResponse>(`v1/payments/${params.id}/mission`)
        .then(({ data }) => {
          if (cancelled) return;
          setMission(data);
          setMissionChecked(true);
          fetchDetail();
          if (!TERMINAL_MISSION_STATES.has(data.mission.state)) {
            timer = setTimeout(fetchMission, MISSION_POLL_MS);
          }
        })
        .catch(() => {
          if (cancelled) return;
          setMission(null);
          setMissionChecked(true);
        });
    };

    fetchDetail();
    fetchMission();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [params.id]);

  if (error) {
    return (
      <main className="page">
        <div className="error-banner">{error}</div>
      </main>
    );
  }
  if (!detail) {
    return (
      <main className="page">
        <p style={{ color: "var(--text-dim)" }}>Loading…</p>
      </main>
    );
  }

  const chosen = detail.candidate_actions.find((c) => c.is_selected);

  return (
    <main className="page">
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
        <h1 style={{ fontSize: "1.4rem" }}>PAYMENT {detail.payment_id.slice(0, 8)}…</h1>
        {mission && (
          <span
            className="badge"
            style={{
              ...missionBadgeStyle(mission.mission.state),
              border: "1px solid",
              background: "transparent",
            }}
          >
            {mission.mission.state.replaceAll("_", " ")}
          </span>
        )}
      </div>

      <dl className="kv" style={{ marginTop: "1rem" }}>
        <dt>Amount</dt>
        <dd>{formatPaise(detail.payment.amount_paise)}</dd>
        <dt>Method / Bank</dt>
        <dd>
          {detail.payment.method} / {detail.payment.bank ?? "—"}
        </dd>
        <dt>Status</dt>
        <dd>{detail.payment.status}</dd>
        <dt>Failure</dt>
        <dd>
          {detail.payment.failure_code ?? "—"} ({detail.payment.failure_class ?? "—"})
        </dd>
      </dl>

      <div className="section-title">Recovery Mission</div>
      {mission ? (
        <div className="chain-step">
          <div className="kv">
            <dt>Objective</dt>
            <dd style={{ fontStyle: "italic" }}>{mission.mission.objective}</dd>
            <dt>Budget</dt>
            <dd>
              {mission.mission.current_attempt}/{mission.mission.max_attempts} attempts ·{" "}
              {mission.mission.current_round}/{mission.mission.max_investigation_rounds} replans
            </dd>
            {mission.mission.ended_at && (
              <>
                <dt>Duration</dt>
                <dd>
                  {(
                    (new Date(mission.mission.ended_at).getTime() -
                      new Date(mission.mission.started_at).getTime()) /
                    1000
                  ).toFixed(1)}
                  s
                </dd>
              </>
            )}
          </div>

          <ol
            style={{
              marginTop: "1rem",
              paddingLeft: "1.25rem",
              borderLeft: "2px solid var(--panel-border)",
              listStyle: "none",
            }}
          >
            {mission.events.map((e) => {
              const isReplan = e.event_type === "REINVESTIGATION_STARTED";
              return (
                <li
                  key={e.sequence_number}
                  style={{
                    position: "relative",
                    marginLeft: "-1.4rem",
                    paddingLeft: "1.4rem",
                    paddingBottom: "0.85rem",
                  }}
                >
                  <span
                    style={{
                      position: "absolute",
                      left: "-5px",
                      top: "0.2rem",
                      width: "8px",
                      height: "8px",
                      borderRadius: "50%",
                      background: isReplan ? "var(--amber)" : "var(--accent)",
                    }}
                  />
                  <div style={{ fontSize: "0.75rem", color: "var(--text-dim)" }}>
                    {ACTOR_LABEL[e.actor] ?? e.actor.toUpperCase()} ·{" "}
                    {new Date(e.created_at).toLocaleTimeString()}
                    {isReplan ? " · REPLANNING" : ""}
                  </div>
                  <div>{eventSummary(e)}</div>
                </li>
              );
            })}
          </ol>
        </div>
      ) : missionChecked ? (
        <p className="step-empty">No recovery mission recorded for this payment yet.</p>
      ) : (
        <p style={{ color: "var(--text-dim)" }}>Loading mission…</p>
      )}

      <div className="section-title">Diagnosis</div>
      {detail.diagnosis ? (
        <div className="chain-step">
          <div>
            <strong>{detail.diagnosis.root_cause}</strong>
          </div>
          <div className="kv" style={{ marginTop: "0.5rem" }}>
            <dt>Confidence</dt>
            <dd>
              {detail.diagnosis.confidence !== null
                ? `${(detail.diagnosis.confidence * 100).toFixed(0)}%`
                : detail.diagnosis.confidence_band ?? "—"}
            </dd>
            <dt>Model</dt>
            <dd>
              {detail.diagnosis.model_version}
              {detail.diagnosis.is_fallback ? " (fallback)" : ""}
            </dd>
          </div>
          {detail.diagnosis.evidence && detail.diagnosis.evidence.length > 0 && (
            <>
              <div style={{ marginTop: "0.75rem", color: "var(--text-dim)", fontSize: "0.85rem" }}>
                Evidence (why this diagnosis — LLM/fallback-authored, plain text only, never
                consulted by the recovery decision below)
              </div>
              <ul style={{ marginTop: "0.35rem", paddingLeft: "1.2rem" }}>
                {detail.diagnosis.evidence.map((e, i) => (
                  <li key={i} style={{ color: "var(--text-dim)", fontSize: "0.9rem" }}>
                    {e.fact}{" "}
                    <span style={{ opacity: 0.6 }}>({e.source})</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ) : (
        <p className="step-empty">No diagnosis recorded for this payment yet.</p>
      )}

      <div className="section-title">AI Recommendation → Fusion</div>
      {detail.ai_fusion ? (
        <div className="chain-step">
          <div className="kv">
            <dt>Deterministic winner</dt>
            <dd>
              {detail.ai_fusion.deterministic_chosen_action} (
              {formatPaise(detail.ai_fusion.deterministic_chosen_evi_paise)})
            </dd>
            <dt>AI recommended</dt>
            <dd>
              {detail.ai_fusion.ai_recommended_action ?? "— (no recommendation available)"}
              {detail.ai_fusion.ai_confidence !== null
                ? ` (confidence ${(detail.ai_fusion.ai_confidence * 100).toFixed(0)}%)`
                : ""}
            </dd>
            {detail.ai_fusion.ai_risk_flags.length > 0 && (
              <>
                <dt>AI risk flags</dt>
                <dd>{detail.ai_fusion.ai_risk_flags.join(", ")}</dd>
              </>
            )}
            <dt>Tolerance</dt>
            <dd>{(detail.ai_fusion.tie_tolerance_bps / 100).toFixed(2)}%</dd>
            <dt>Final action</dt>
            <dd>
              <strong>{detail.ai_fusion.final_action}</strong>
              {detail.ai_fusion.tie_break_applied ? " (AI tie-break accepted)" : ""}
              {detail.ai_fusion.risk_escalation_applied ? " (AI risk escalation)" : ""}
            </dd>
          </div>
          {detail.ai_fusion.near_tied_candidates.length > 0 && (
            <div style={{ marginTop: "0.5rem", color: "var(--text-dim)", fontSize: "0.85rem" }}>
              Near-tied candidates (within tolerance):{" "}
              {detail.ai_fusion.near_tied_candidates
                .map((c) => `${c.action_type} (${formatPaise(c.evi_paise)})`)
                .join(", ")}
            </div>
          )}
          <div style={{ marginTop: "0.5rem", color: "var(--text-dim)", fontSize: "0.9rem" }}>
            Why: {detail.ai_fusion.fusion_reason}
          </div>
        </div>
      ) : (
        <p className="step-empty">
          AI recommendation fusion was not enabled for this decision — chosen_action came from
          deterministic EVI/policy alone.
        </p>
      )}

      <div className="section-title">Candidate Actions</div>
      {detail.candidate_actions.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>No candidate actions recorded.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Action</th>
              <th>Probability</th>
              <th>Expected Value</th>
              <th>Cost</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {detail.candidate_actions.map((c) => (
              <tr key={c.candidate_id}>
                <td>{c.action_type}</td>
                <td>{formatBps(c.recovery_prob_bps)}</td>
                <td>{formatPaise(c.expected_value_paise)}</td>
                <td>{formatPaise(c.cost_paise)}</td>
                <td>
                  {c.is_selected && <span className="badge selected">SELECTED</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {chosen && (
        <p style={{ marginTop: "0.75rem", color: "var(--text-dim)" }}>
          Expected Recovery: {formatPaise(chosen.expected_value_paise)}
        </p>
      )}

      <div className="section-title">Policy</div>
      {detail.policy_decision ? (
        <div className="chain-step">
          <div className="kv">
            <dt>Verdict</dt>
            <dd>{detail.policy_decision.verdict}</dd>
            <dt>Stopping Rule</dt>
            <dd>{detail.policy_decision.stopping_rule ?? "—"}</dd>
          </div>
          <ul style={{ marginTop: "0.75rem", paddingLeft: "1.2rem" }}>
            {detail.policy_decision.rule_trace.map((r, i) => (
              <li key={i} style={{ color: r.passed ? "var(--green)" : "var(--red)" }}>
                {r.passed ? "✓" : "✗"} {r.rule}: {r.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="step-empty">No policy decision recorded yet.</p>
      )}

      <div className="section-title">Recovery History</div>
      {detail.recovery_history.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>No execution attempts yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Attempt</th>
              <th>Action</th>
              <th>Outcome</th>
              <th>Recovered</th>
              <th>Executed At</th>
            </tr>
          </thead>
          <tbody>
            {detail.recovery_history.map((r) => (
              <tr key={r.recovery_id}>
                <td>{r.attempt_number}</td>
                <td>{r.action_type}</td>
                <td>{r.outcome ?? "PENDING"}</td>
                <td>{formatPaise(r.recovered_amount_paise)}</td>
                <td>{r.executed_at ? new Date(r.executed_at).toLocaleString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
