import { NextRequest, NextResponse } from "next/server";

/**
 * Thin server-side proxy to the FastAPI backend. Exists so the real
 * X-API-Key (recoveryos/apps/api/dependencies/auth.py) never reaches the
 * browser — only this route reads API_KEY (server-only env var, no
 * NEXT_PUBLIC_ prefix). Every number the dashboard renders comes from
 * whatever this forwards back verbatim, including X-Model-Version /
 * X-Policy-Version (TRD §5) so the footer badge is real, not decorative.
 */

const API_BASE_URL = process.env.API_BASE_URL || "http://localhost:8000";
const API_KEY = process.env.API_KEY || "";

async function forward(req: NextRequest, path: string[]): Promise<NextResponse> {
  const url = `${API_BASE_URL}/${path.join("/")}${req.nextUrl.search}`;

  const init: RequestInit = {
    method: req.method,
    headers: {
      "X-API-Key": API_KEY,
      "Content-Type": "application/json",
    },
  };

  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch (err) {
    return NextResponse.json(
      { detail: `Backend unreachable at ${API_BASE_URL}: ${(err as Error).message}` },
      { status: 502 }
    );
  }

  const body = await upstream.text();
  const res = new NextResponse(body, { status: upstream.status });
  res.headers.set(
    "content-type",
    upstream.headers.get("content-type") || "application/json"
  );

  const modelVersion = upstream.headers.get("x-model-version");
  const policyVersion = upstream.headers.get("x-policy-version");
  if (modelVersion) res.headers.set("x-model-version", modelVersion);
  if (policyVersion) res.headers.set("x-policy-version", policyVersion);

  return res;
}

export async function GET(
  req: NextRequest,
  { params }: { params: { path: string[] } }
) {
  return forward(req, params.path);
}

export async function POST(
  req: NextRequest,
  { params }: { params: { path: string[] } }
) {
  return forward(req, params.path);
}
