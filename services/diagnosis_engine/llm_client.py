"""
Shared low-level Gemini REST call -- factored out of llm_diagnoser.py so
services/diagnosis_engine/investigator.py's multi-round investigation loop
(Task AGENT1) doesn't duplicate the same HTTP/schema-stripping logic.
llm_diagnoser.py's own _call_llm_gemini is a thin wrapper around this now;
its external behavior (and every existing test) is unchanged.
"""

from __future__ import annotations

import json


def strip_additional_properties(schema: dict) -> dict:
    """Gemini's responseSchema dialect doesn't accept `additionalProperties`
    (some SDK/API versions reject it outright). Recursive strip, not a
    hand-maintained duplicate schema, so a caller's real OpenAI-style schema
    can never silently drift from what's actually sent to Gemini."""

    def _strip(node):
        if isinstance(node, dict):
            return {k: _strip(v) for k, v in node.items() if k != "additionalProperties"}
        if isinstance(node, list):
            return [_strip(v) for v in node]
        return node

    return _strip(schema)


async def gemini_generate_json(
    *,
    system_prompt: str,
    user_content: dict,
    response_schema: dict,
    model: str,
    api_key: str,
) -> dict:
    """
    One Gemini generateContent call, constrained to response_schema (already
    Gemini-dialect -- pass it through strip_additional_properties() first if
    it came from an OpenAI-style schema). No internal timeout here -- the
    caller wraps this in asyncio.wait_for with its own budget, exactly like
    llm_diagnoser.diagnose_with_llm() already does for the single-call path.
    """
    import httpx

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(user_content)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, params={"key": api_key}, json=body, timeout=None)
        response.raise_for_status()
        data = response.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)
