"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, ApiError, formatPaise } from "@/lib/api";

type Incident = {
  window_id: string;
  bank: string;
  time_bucket: string;
  baseline_success_rate: number | null;
  observed_success_rate: number | null;
  z_score: number | null;
  affected_payment_count: number;
  revenue_at_risk_paise: number;
  expected_recovery_paise: number;
  recommended_action: string;
  root_cause: string;
};

const POLL_MS = 5000;

export default function IncidentsPage() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { data } = await apiGet<{ incidents: Incident[] }>("v1/incidents/active");
      setIncidents(data.incidents);
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

  return (
    <main className="page">
      <h1 style={{ fontSize: "1.4rem" }}>System Incidents</h1>
      {error && <div className="error-banner">{error}</div>}

      {incidents.length === 0 && !error && (
        <p style={{ color: "var(--text-dim)", marginTop: "1rem" }}>
          No active high-severity anomalies. Trigger one from the Control Tower&apos;s
          &quot;SIMULATE DEGRADATION&quot; button (demo mode only) to see this screen populate.
        </p>
      )}

      {incidents.map((incident) => (
        <div className="chain-step" key={incident.window_id} style={{ marginTop: "1rem" }}>
          <div className="step-label">SYSTEMIC DEGRADATION</div>
          <h2 style={{ margin: "0 0 0.75rem" }}>{incident.bank}</h2>
          <dl className="kv">
            <dt>Success rate</dt>
            <dd>
              {incident.baseline_success_rate !== null
                ? `${(incident.baseline_success_rate * 100).toFixed(1)}%`
                : "—"}{" "}
              →{" "}
              {incident.observed_success_rate !== null
                ? `${(incident.observed_success_rate * 100).toFixed(1)}%`
                : "—"}
            </dd>
            <dt>Affected</dt>
            <dd>{incident.affected_payment_count.toLocaleString()} payments</dd>
            <dt>Revenue at risk</dt>
            <dd>{formatPaise(incident.revenue_at_risk_paise)}</dd>
            <dt>Root cause</dt>
            <dd>{incident.root_cause}</dd>
            <dt>Recommended action</dt>
            <dd>{incident.recommended_action}</dd>
            <dt>Expected recovery</dt>
            <dd>{formatPaise(incident.expected_recovery_paise)}</dd>
            <dt>Z-score</dt>
            <dd>{incident.z_score?.toFixed(2) ?? "—"}</dd>
          </dl>
        </div>
      ))}
    </main>
  );
}
