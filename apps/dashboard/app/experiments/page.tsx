"use client";

import { useEffect, useState } from "react";
import { apiGet, ApiError, formatPaise } from "@/lib/api";

type AiContribution = {
  recommendations_available: number;
  near_tie_decisions: number;
  ai_tie_breaks: number;
  risk_escalations: number;
  ai_outcome_delta_total: number;
  ai_outcome_delta_rate: number | null;
};

type LiveExperiment = {
  run_id: "live";
  dataset_size: number;
  baseline: { recovered_paise: number; interventions: number };
  recoveryos: { recovered_paise: number; interventions: number; unnecessary_interventions: number };
  incremental_recovery_paise: number;
  recovery_rate_bps: number;
  ai_contribution: AiContribution;
};

type Phase8Seed = {
  seed: number;
  failed_payments: number;
  recoveryos_total_paise: number;
  baseline_total_paise: number;
  incremental_recovery_paise: number;
  recovery_rate: number;
};

type Phase8Experiment = {
  run_id: "phase8-baseline";
  dataset_size: number;
  seeds: Phase8Seed[];
  baseline: { recovered_paise: number };
  recoveryos: {
    recovered_paise: number;
    recovery_rate_bps: number;
    intervention_rate_bps: number;
    unnecessary_intervention_rate_bps: number;
  };
  incremental_recovery_paise_mean: number;
  incremental_recovery_paise_stdev: number;
  incremental_recovery_95ci_paise: [number, number];
};

export default function ExperimentsPage() {
  const [live, setLive] = useState<LiveExperiment | null>(null);
  const [phase8, setPhase8] = useState<Phase8Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<LiveExperiment>("v1/experiments/live")
      .then(({ data }) => setLive(data))
      .catch(() => {});
    apiGet<Phase8Experiment>("v1/experiments/phase8-baseline")
      .then(({ data }) => setPhase8(data))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load."));
  }, []);

  return (
    <main className="page">
      <h1 style={{ fontSize: "1.4rem" }}>Recovery Experiment</h1>
      {error && <div className="error-banner">{error}</div>}

      {phase8 && (
        <>
          <p style={{ color: "var(--text-dim)" }}>
            Phase 8 multi-seed replication study — {phase8.seeds.length} independent runs,{" "}
            {phase8.dataset_size.toLocaleString()} failed payments each.
          </p>
          <table style={{ marginTop: "1rem" }}>
            <thead>
              <tr>
                <th></th>
                <th>Baseline</th>
                <th>RecoveryOS</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Recovered (mean)</td>
                <td>{formatPaise(phase8.baseline.recovered_paise)}</td>
                <td>{formatPaise(phase8.recoveryos.recovered_paise)}</td>
              </tr>
              <tr>
                <td>Recovery rate</td>
                <td>—</td>
                <td>{(phase8.recoveryos.recovery_rate_bps / 100).toFixed(1)}%</td>
              </tr>
              <tr>
                <td>Intervention rate</td>
                <td>100%</td>
                <td>{(phase8.recoveryos.intervention_rate_bps / 100).toFixed(1)}%</td>
              </tr>
              <tr>
                <td>Unnecessary interventions</td>
                <td>—</td>
                <td>{(phase8.recoveryos.unnecessary_intervention_rate_bps / 100).toFixed(1)}%</td>
              </tr>
            </tbody>
          </table>
          <p style={{ marginTop: "1rem" }}>
            <strong>
              RecoveryOS generated {formatPaise(phase8.incremental_recovery_paise_mean)} mean
              incremental recovered revenue
            </strong>{" "}
            across {phase8.seeds.length} independent 10k-payment runs — 95% CI [
            {formatPaise(phase8.incremental_recovery_95ci_paise[0])},{" "}
            {formatPaise(phase8.incremental_recovery_95ci_paise[1])}].
          </p>

          <div className="section-title" style={{ marginTop: "1.5rem" }}>
            Per-seed breakdown
          </div>
          <p style={{ color: "var(--text-dim)" }}>
            The mean and CI above average across every run below, including any run where
            RecoveryOS recovered less than the baseline — that variance is not hidden here.
          </p>
          <table style={{ marginTop: "0.5rem" }}>
            <thead>
              <tr>
                <th>Seed</th>
                <th>Failed payments</th>
                <th>Baseline recovered</th>
                <th>RecoveryOS recovered</th>
                <th>Incremental</th>
              </tr>
            </thead>
            <tbody>
              {phase8.seeds.map((s) => {
                const negative = s.incremental_recovery_paise < 0;
                return (
                  <tr key={s.seed}>
                    <td>{s.seed}</td>
                    <td>{s.failed_payments.toLocaleString()}</td>
                    <td>{formatPaise(s.baseline_total_paise)}</td>
                    <td>{formatPaise(s.recoveryos_total_paise)}</td>
                    <td style={negative ? { color: "var(--danger, #d33)" } : undefined}>
                      {negative ? "" : "+"}
                      {formatPaise(s.incremental_recovery_paise)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      <div className="section-title">This merchant&apos;s live traffic</div>
      {live && live.dataset_size > 0 ? (
        <table>
          <thead>
            <tr>
              <th></th>
              <th>Baseline</th>
              <th>RecoveryOS</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Dataset</td>
              <td colSpan={2}>{live.dataset_size.toLocaleString()} payments</td>
            </tr>
            <tr>
              <td>Recovered</td>
              <td>{formatPaise(live.baseline.recovered_paise)}</td>
              <td>{formatPaise(live.recoveryos.recovered_paise)}</td>
            </tr>
            <tr>
              <td>Interventions</td>
              <td>{live.baseline.interventions}</td>
              <td>{live.recoveryos.interventions}</td>
            </tr>
            <tr>
              <td>Incremental</td>
              <td colSpan={2}>{formatPaise(live.incremental_recovery_paise)}</td>
            </tr>
          </tbody>
        </table>
      ) : (
        <p style={{ color: "var(--text-dim)" }}>
          No synthetic traffic processed by this merchant yet — this section populates once
          payments with simulator ground truth run through the live pipeline.
        </p>
      )}

      {live && (
        <>
          <div className="section-title">AI Contribution</div>
          <p style={{ color: "var(--text-dim)" }}>
            Bounded AI fusion only ever wins an economic tie-break already cleared by EVI/policy,
            or triggers a deterministic escalation rule — it can never invent an action. These
            counts are real, from every decision this merchant&apos;s traffic has produced.
          </p>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="label">Recommendations Available</div>
              <div className="value">{live.ai_contribution.recommendations_available}</div>
            </div>
            <div className="stat-card">
              <div className="label">Near-Tie Decisions</div>
              <div className="value">{live.ai_contribution.near_tie_decisions}</div>
            </div>
            <div className="stat-card">
              <div className="label">AI Tie-Breaks</div>
              <div className="value">{live.ai_contribution.ai_tie_breaks}</div>
            </div>
            <div className="stat-card">
              <div className="label">Risk Escalations</div>
              <div className="value">{live.ai_contribution.risk_escalations}</div>
            </div>
            <div className="stat-card">
              <div className="label">AI Changed Final Outcome</div>
              <div className="value positive">{live.ai_contribution.ai_outcome_delta_total}</div>
            </div>
            <div className="stat-card">
              <div className="label">AI Outcome-Change Rate</div>
              <div className="value">
                {live.ai_contribution.ai_outcome_delta_rate !== null
                  ? `${(live.ai_contribution.ai_outcome_delta_rate * 100).toFixed(1)}%`
                  : "—"}
              </div>
            </div>
          </div>
        </>
      )}

      <div className="section-title">AI Authority Boundary</div>
      <div className="chain-step">
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.75rem" }}>
          <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", justifyContent: "center" }}>
            {["Diagnose", "Recommend", "Risk Signal"].map((label) => (
              <div
                key={label}
                style={{
                  border: "1px solid var(--accent)",
                  borderRadius: "8px",
                  padding: "0.5rem 1rem",
                  fontSize: "0.85rem",
                  color: "var(--accent)",
                }}
              >
                AI: {label}
              </div>
            ))}
          </div>
          <div style={{ color: "var(--text-dim)", fontSize: "1.2rem" }}>↓</div>
          <div
            style={{
              border: "1px solid var(--text)",
              borderRadius: "8px",
              padding: "0.6rem 1.5rem",
              fontWeight: 700,
              letterSpacing: "0.03em",
            }}
          >
            DETERMINISTIC CONTROL PLANE (EVI + Policy)
          </div>
          <div style={{ color: "var(--text-dim)", fontSize: "1.2rem" }}>↓</div>
          <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", justifyContent: "center" }}>
            <div
              style={{
                border: "1px solid var(--green)",
                borderRadius: "8px",
                padding: "0.5rem 1rem",
                fontSize: "0.85rem",
                color: "var(--green)",
              }}
            >
              ALLOWED → EXECUTION
            </div>
            <div
              style={{
                border: "1px solid var(--red)",
                borderRadius: "8px",
                padding: "0.5rem 1rem",
                fontSize: "0.85rem",
                color: "var(--red)",
              }}
            >
              BLOCKED
            </div>
          </div>
        </div>
        <ul style={{ marginTop: "1.25rem", paddingLeft: "1.25rem", fontSize: "0.9rem", color: "var(--text-dim)" }}>
          <li>✓ AI cannot bypass EVI — it may only win a tie already inside the disclosed tolerance.</li>
          <li>✓ AI cannot bypass policy — every candidate it can pick from was already policy-cleared.</li>
          <li>✓ AI cannot set execution parameters — it never supplies amounts, routes, or timing directly.</li>
        </ul>
      </div>
    </main>
  );
}
