"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function AuditSearchPage() {
  const [paymentId, setPaymentId] = useState("");
  const router = useRouter();

  function go(e: React.FormEvent) {
    e.preventDefault();
    if (paymentId.trim()) {
      router.push(`/audit/${paymentId.trim()}`);
    }
  }

  return (
    <main className="page">
      <h1 style={{ fontSize: "1.4rem" }}>Audit Explorer</h1>
      <p style={{ color: "var(--text-dim)" }}>
        Enter a payment ID to replay its full decision chain: PAYMENT → FAILURE → ANOMALY →
        DIAGNOSIS → PROPENSITY → ACTIONS → EVI → POLICY → EXECUTION → OUTCOME.
      </p>
      <form onSubmit={go} style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
        <input
          style={{ flex: 1 }}
          placeholder="payment_id (uuid)"
          value={paymentId}
          onChange={(e) => setPaymentId(e.target.value)}
        />
        <button className="primary" type="submit">
          Open
        </button>
      </form>
    </main>
  );
}
