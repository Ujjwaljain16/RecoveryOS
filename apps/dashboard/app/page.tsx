"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiGet, apiPost, ApiError, formatBps, formatPaise } from "@/lib/api";

type BankHealth = {
  bank: string;
  status: "HEALTHY" | "DEGRADED";
  severity: string | null;
  observed_rate: number | null;
  baseline_rate: number | null;
  time_bucket: string;
};

type QueueItem = {
  payment_id: string;
  amount_paise: number;
  chosen_action: string;
  recovery_prob_bps: number;
  status: "EXECUTING" | "SCHEDULED";
  updated_at: string;
};

type RiskSummary = {
  revenue_at_risk_paise: number;
  recovered_paise: number;
  incremental_recovery_paise: number;
  recovery_rate_bps: number;
  bank_health: BankHealth[];
  recovery_queue: QueueItem[];
};

const POLL_MS = 3000;

export default function ControlTowerPage() {
  const [summary, setSummary] = useState<RiskSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDemo, setIsDemo] = useState(false);
  const [degrading, setDegrading] = useState(false);
  const [degradeResult, setDegradeResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<RiskSummary>("v1/risk/summary");
      setSummary(data);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to reach the backend.");
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, POLL_MS);
    return () => clearInterval(interval);
  }, [load]);

  useEffect(() => {
    fetch("/api/proxy/health", { cache: "no-store" })
      .then((res) => res.json())
      .then((body) => setIsDemo(body.env === "demo"))
      .catch(() => setIsDemo(false));
  }, []);

  async function handleSimulateDegrade() {
    setDegrading(true);
    setDegradeResult(null);
    try {
      const bank = summary?.bank_health[0]?.bank || "HDFC";
      const { data } = await apiPost<{
        anomaly_detection_result: { severity: string; z_score: number | null; is_anomaly: boolean };
      }>("v1/simulate/degrade", {
        bank,
        method: "upi",
        target_success_rate: 0.4,
        duration_minutes: 15,
      });
      const r = data.anomaly_detection_result;
      setDegradeResult(
        `Injected real degradation for ${bank}. Detector result: severity=${r.severity}, ` +
          `z=${r.z_score?.toFixed(2) ?? "n/a"}, is_anomaly=${r.is_anomaly}.`
      );
      await load();
    } catch (err) {
      setDegradeResult(
        err instanceof ApiError ? `Failed: ${err.message}` : "Failed to reach the backend."
      );
    } finally {
      setDegrading(false);
    }
  }

  return (
    <main className="page">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ fontSize: "1.4rem" }}>Control Tower</h1>
        <button
          className="primary"
          disabled={!isDemo || degrading}
          onClick={handleSimulateDegrade}
          title={isDemo ? "Inject a real bank degradation and watch the detector fire" : "Only enabled when ENV=demo"}
        >
          {degrading ? "Simulating…" : "SIMULATE DEGRADATION"}
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {degradeResult && <div className="error-banner" style={{ color: "var(--accent)", borderColor: "var(--accent)" }}>{degradeResult}</div>}

      {summary && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="label">Revenue at Risk</div>
              <div className="value">{formatPaise(summary.revenue_at_risk_paise)}</div>
            </div>
            <div className="stat-card">
              <div className="label">Recovered</div>
              <div className="value positive">{formatPaise(summary.recovered_paise)}</div>
            </div>
            <div className="stat-card">
              <div className="label">Incremental Recovery</div>
              <div
                className={`value ${summary.incremental_recovery_paise >= 0 ? "positive" : "negative"}`}
              >
                {summary.incremental_recovery_paise >= 0 ? "+" : ""}
                {formatPaise(summary.incremental_recovery_paise)}
              </div>
            </div>
            <div className="stat-card">
              <div className="label">Recovery Rate</div>
              <div className="value">{formatBps(summary.recovery_rate_bps)}</div>
            </div>
          </div>

          <div className="section-title">System Health</div>
          {summary.bank_health.length === 0 ? (
            <p style={{ color: "var(--text-dim)" }}>
              No anomaly windows computed yet — bank health has no data.
            </p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Bank</th>
                  <th>Status</th>
                  <th>Observed Rate</th>
                  <th>Baseline Rate</th>
                  <th>Last Updated</th>
                </tr>
              </thead>
              <tbody>
                {summary.bank_health.map((b) => (
                  <tr key={b.bank}>
                    <td>{b.bank}</td>
                    <td>
                      <span
                        className={`badge ${b.status === "HEALTHY" ? "healthy" : "degraded"}`}
                      >
                        {b.status}
                      </span>
                    </td>
                    <td>{b.observed_rate !== null ? `${(b.observed_rate * 100).toFixed(1)}%` : "—"}</td>
                    <td>{b.baseline_rate !== null ? `${(b.baseline_rate * 100).toFixed(1)}%` : "—"}</td>
                    <td>{new Date(b.time_bucket).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="section-title">Recovery Queue</div>
          {summary.recovery_queue.length === 0 ? (
            <p style={{ color: "var(--text-dim)" }}>Queue is empty.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Payment</th>
                  <th>Amount</th>
                  <th>Action</th>
                  <th>Probability</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {summary.recovery_queue.map((q) => (
                  <tr key={q.payment_id}>
                    <td>
                      <Link href={`/audit/${q.payment_id}`}>{q.payment_id.slice(0, 8)}…</Link>
                    </td>
                    <td>{formatPaise(q.amount_paise)}</td>
                    <td>{q.chosen_action}</td>
                    <td>{formatBps(q.recovery_prob_bps)}</td>
                    <td>
                      <span className="badge neutral">{q.status}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </main>
  );
}
