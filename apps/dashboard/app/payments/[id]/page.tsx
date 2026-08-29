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
};

export default function PaymentDetailPage({ params }: { params: { id: string } }) {
  const [detail, setDetail] = useState<PaymentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<PaymentDetail>(`v1/payments/${params.id}/detail`)
      .then(({ data }) => setDetail(data))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load payment."));
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
      <h1 style={{ fontSize: "1.4rem" }}>PAYMENT {detail.payment_id.slice(0, 8)}…</h1>

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
