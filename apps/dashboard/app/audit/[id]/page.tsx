"use client";

import { useEffect, useState } from "react";
import { apiGet, ApiError, formatPaise } from "@/lib/api";

type Chain = {
  payment: { amount_paise: number; method: string; bank: string | null; status: string; created_at: string };
  failure: { failure_code: string | null; failure_class: string | null; failed_at: string | null };
  anomaly: {
    scope_entity: string;
    severity: string | null;
    z_score: number | null;
    observed_rate: number | null;
    baseline_rate: number | null;
  } | null;
  diagnosis: { root_cause: string; confidence: number | null; confidence_band: string | null } | null;
  propensity: { recovery_prob_bps: number; model_version: string; source: string } | null;
  actions: { candidate_id: string; action_type: string; recovery_prob_bps: number; is_selected: boolean }[];
  evi: {
    action_type: string;
    expected_value_paise: number;
    cost_paise: number;
    friction_penalty_paise: number;
    risk_penalty_paise: number;
  } | null;
  policy: { verdict: string; stopping_rule: string | null } | null;
  execution: { attempt_number: number; action_type: string; outcome: string | null }[];
  outcome: {
    revenue_at_risk_paise: number;
    actual_recovery_paise: number;
    incremental_recovery_paise: number;
    baseline_outcome: string | null;
  } | null;
};

type AuditResponse = {
  payment_id: string;
  chain: Chain;
  audit_log: { audit_id: string; summary: string; created_at: string }[];
};

function Step({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="chain-step">
      <div className="step-label">{label}</div>
      {children}
    </div>
  );
}

export default function AuditDetailPage({ params }: { params: { id: string } }) {
  const [data, setData] = useState<AuditResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<AuditResponse>(`v1/audit/${params.id}`)
      .then(({ data }) => setData(data))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load audit trail."));
  }, [params.id]);

  if (error) {
    return (
      <main className="page">
        <div className="error-banner">{error}</div>
      </main>
    );
  }
  if (!data) {
    return (
      <main className="page">
        <p style={{ color: "var(--text-dim)" }}>Loading…</p>
      </main>
    );
  }

  const { chain } = data;

  return (
    <main className="page">
      <h1 style={{ fontSize: "1.4rem" }}>Audit Chain — {data.payment_id.slice(0, 8)}…</h1>

      <Step label="1. Payment">
        <div className="kv">
          <dt>Amount</dt>
          <dd>{formatPaise(chain.payment.amount_paise)}</dd>
          <dt>Method / Bank</dt>
          <dd>
            {chain.payment.method} / {chain.payment.bank ?? "—"}
          </dd>
        </div>
      </Step>

      <Step label="2. Failure">
        <div className="kv">
          <dt>Code</dt>
          <dd>{chain.failure.failure_code ?? "—"}</dd>
          <dt>Class</dt>
          <dd>{chain.failure.failure_class ?? "—"}</dd>
        </div>
      </Step>

      <Step label="3. Anomaly">
        {chain.anomaly ? (
          <div className="kv">
            <dt>Bank</dt>
            <dd>{chain.anomaly.scope_entity}</dd>
            <dt>Severity</dt>
            <dd>{chain.anomaly.severity}</dd>
            <dt>Z-score</dt>
            <dd>{chain.anomaly.z_score?.toFixed(2) ?? "—"}</dd>
          </div>
        ) : (
          <div className="step-empty">No anomaly window active for this payment&apos;s bucket.</div>
        )}
      </Step>

      <Step label="4. Diagnosis">
        {chain.diagnosis ? (
          <div className="kv">
            <dt>Root cause</dt>
            <dd>{chain.diagnosis.root_cause}</dd>
            <dt>Confidence</dt>
            <dd>
              {chain.diagnosis.confidence !== null
                ? `${(chain.diagnosis.confidence * 100).toFixed(0)}%`
                : chain.diagnosis.confidence_band ?? "—"}
            </dd>
          </div>
        ) : (
          <div className="step-empty">No diagnosis recorded.</div>
        )}
      </Step>

      <Step label="5. Recovery Propensity">
        {chain.propensity ? (
          <div className="kv">
            <dt>Probability</dt>
            <dd>{(chain.propensity.recovery_prob_bps / 100).toFixed(1)}%</dd>
            <dt>Model</dt>
            <dd>{chain.propensity.model_version}</dd>
          </div>
        ) : (
          <div className="step-empty">No RETRY_NOW candidate scored.</div>
        )}
      </Step>

      <Step label="6. Action Options">
        {chain.actions.length === 0 ? (
          <div className="step-empty">No candidate actions.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Action</th>
                <th>Probability</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {chain.actions.map((a) => (
                <tr key={a.candidate_id}>
                  <td>{a.action_type}</td>
                  <td>{(a.recovery_prob_bps / 100).toFixed(1)}%</td>
                  <td>{a.is_selected && <span className="badge selected">SELECTED</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Step>

      <Step label="7. Expected Value (EVI)">
        {chain.evi ? (
          <div className="kv">
            <dt>Action</dt>
            <dd>{chain.evi.action_type}</dd>
            <dt>Expected value</dt>
            <dd>{formatPaise(chain.evi.expected_value_paise)}</dd>
            <dt>Cost</dt>
            <dd>{formatPaise(chain.evi.cost_paise)}</dd>
          </div>
        ) : (
          <div className="step-empty">No EVI recorded.</div>
        )}
      </Step>

      <Step label="8. Policy Check">
        {chain.policy ? (
          <div className="kv">
            <dt>Verdict</dt>
            <dd>{chain.policy.verdict}</dd>
            <dt>Stopping rule</dt>
            <dd>{chain.policy.stopping_rule ?? "—"}</dd>
          </div>
        ) : (
          <div className="step-empty">No policy decision recorded.</div>
        )}
      </Step>

      <Step label="9. Execution">
        {chain.execution.length === 0 ? (
          <div className="step-empty">No execution attempts yet.</div>
        ) : (
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {chain.execution.map((e) => (
              <li key={e.attempt_number}>
                Attempt {e.attempt_number}: {e.action_type} → {e.outcome ?? "PENDING"}
              </li>
            ))}
          </ul>
        )}
      </Step>

      <Step label="10. Outcome">
        {chain.outcome ? (
          <div className="kv">
            <dt>Revenue at risk</dt>
            <dd>{formatPaise(chain.outcome.revenue_at_risk_paise)}</dd>
            <dt>Actual recovery</dt>
            <dd>{formatPaise(chain.outcome.actual_recovery_paise)}</dd>
            <dt>Incremental</dt>
            <dd>{formatPaise(chain.outcome.incremental_recovery_paise)}</dd>
          </div>
        ) : (
          <div className="step-empty">No terminal ledger row yet.</div>
        )}
      </Step>

      <div className="section-title">Audit Log</div>
      <ul style={{ paddingLeft: "1.2rem" }}>
        {data.audit_log.map((a) => (
          <li key={a.audit_id}>
            <span style={{ color: "var(--text-dim)" }}>
              {new Date(a.created_at).toLocaleString()}
            </span>{" "}
            — {a.summary}
          </li>
        ))}
      </ul>
    </main>
  );
}
