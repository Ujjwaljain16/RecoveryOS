"use client";

import { useEffect, useState } from "react";

/**
 * TRD §5: "all responses include X-Model-Version and X-Policy-Version
 * headers so any dashboard screenshot is reproducible against the exact
 * model/policy that produced it." This reads those headers off a real
 * request (not the response body — the middleware injects them on every
 * response, so /health carries them exactly like every data endpoint) and
 * surfaces them as a small credibility footer.
 */
export default function VersionFooter() {
  const [modelVersion, setModelVersion] = useState<string | null>(null);
  const [policyVersion, setPolicyVersion] = useState<string | null>(null);
  const [env, setEnv] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch("/api/proxy/health", { cache: "no-store" });
        if (cancelled) return;
        setModelVersion(res.headers.get("x-model-version"));
        setPolicyVersion(res.headers.get("x-policy-version"));
        const body = await res.json();
        setEnv(body.env ?? null);
      } catch {
        if (!cancelled) {
          setModelVersion(null);
          setPolicyVersion(null);
        }
      }
    }
    load();
    const interval = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return (
    <div className="footer">
      <span>Model: {modelVersion ?? "…"}</span>
      <span>Policy: {policyVersion ?? "…"}</span>
      <span>Env: {env ?? "…"}</span>
    </div>
  );
}
