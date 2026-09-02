"""
AI Architecture Gap Audit gap (P2): every existing investigator/diagnoser
test mocks gemini_generate_json() itself (services/diagnosis_engine/
llm_client.py) -- its own body (the httpx call, the response.json() parse,
the data["candidates"][0]["content"]["parts"][0]["text"] extraction, the
json.loads(text) of that inner text) had never been exercised directly.
This is the first test file to do that: mock httpx.AsyncClient.post (the one
network call inside gemini_generate_json) and prove the function raises the
REAL exception type for two genuinely malformed Gemini response shapes,
rather than assuming it does.

investigate()'s own fail-closed handling of these exact exception types is
covered separately in test_investigator.py's
test_finalize_call_raising_json_decode_error_falls_back_cleanly /
test_finalize_call_raising_key_error_falls_back_cleanly -- this file only
proves gemini_generate_json's own parsing behavior in isolation.
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.diagnosis_engine.llm_client import gemini_generate_json


def _patch_post(monkeypatch, response_json: dict):
    async def _fake_post(self, url, *, params=None, json=None, timeout=None):
        request = httpx.Request("POST", url, params=params)
        return httpx.Response(200, request=request, json=response_json)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)


@pytest.mark.asyncio
async def test_non_json_inner_text_raises_json_decode_error(monkeypatch):
    """Gemini's OUTER envelope is well-formed (a real 200, a real
    candidates[0].content.parts[0].text field) but the schema-constrained
    TEXT that field carries is not valid JSON -- a genuinely malformed
    structured-output response, not just a schema-shape mismatch."""
    inner_text = "not valid json{{{"
    response_json = {"candidates": [{"content": {"parts": [{"text": inner_text}]}}]}
    _patch_post(monkeypatch, response_json)

    with pytest.raises(json.JSONDecodeError):
        await gemini_generate_json(
            system_prompt="sys",
            user_content={"k": "v"},
            response_schema={"type": "object"},
            model="gemini-2.5-flash-lite",
            api_key="fake",
        )


@pytest.mark.asyncio
async def test_safety_blocked_response_missing_candidates_raises_key_error(monkeypatch):
    """A real Gemini safety-block shape: no `candidates` key at all, only
    `promptFeedback`. data["candidates"] must raise KeyError, not silently
    return something diagnose-able."""
    _patch_post(monkeypatch, {"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(KeyError):
        await gemini_generate_json(
            system_prompt="sys",
            user_content={"k": "v"},
            response_schema={"type": "object"},
            model="gemini-2.5-flash-lite",
            api_key="fake",
        )


@pytest.mark.asyncio
async def test_empty_candidates_list_raises_index_error(monkeypatch):
    """A `candidates` key present but empty (e.g. every candidate filtered)
    -- data["candidates"][0] must raise IndexError."""
    _patch_post(monkeypatch, {"candidates": []})

    with pytest.raises(IndexError):
        await gemini_generate_json(
            system_prompt="sys",
            user_content={"k": "v"},
            response_schema={"type": "object"},
            model="gemini-2.5-flash-lite",
            api_key="fake",
        )
