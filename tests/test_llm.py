"""Tests for the LLM provider layer, focused on the OpenAI-compatible path.

The HTTP call is mocked, so provider selection and response parsing are exercised
offline and deterministically.
"""

from __future__ import annotations

import json

import pytest

from src.agent import llm
from src.agent.tools import TOOL_CATALOG
from src.payments import config


def _sse(*chunks: dict) -> bytes:
    """Render chat-completion chunks as an SSE body, terminated with [DONE].

    The gateway only serves streaming responses, so the mocked endpoint returns the
    same `data: {...}` line format the real one does and llm._openai_post reassembles.
    """
    body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


@pytest.fixture
def openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://llm.example/v1")
    monkeypatch.setattr(config, "OPENAI_MODEL", "test-model")


def test_provider_autodetects_openai(openai_env: None) -> None:
    assert llm.provider() == "openai"
    assert llm.available() is True


def test_provider_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "")
    assert llm.provider() == "none"
    assert llm.available() is False


def test_explicit_provider_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "x")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://y/v1")
    assert llm.provider() == "anthropic"


def test_forced_provider_without_credentials_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # LLM_PROVIDER=openai but no base URL must not pass available() then crash.
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "x")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert llm.provider() == "none"
    assert llm.available() is False


def test_forced_provider_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LLM_PROVIDER", "OpenAI")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "x")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://y/v1")
    assert llm.provider() == "openai"


def test_unknown_forced_provider_falls_back_to_autodetect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "LLM_PROVIDER", "gpt5")  # not a real provider
    monkeypatch.setattr(config, "OPENAI_API_KEY", "x")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://y/v1")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert llm.provider() == "openai"


async def test_openai_chat_returns_trimmed_content(
    openai_env: None,
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    httpx_mock.add_response(
        content=_sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "  hello"}}]},
            {"choices": [{"delta": {"content": " world  "}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )
    out = await llm.chat("hi", max_tokens=10)
    assert out == "hello world"


async def test_openai_plan_picks_a_tool(
    openai_env: None,
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    httpx_mock.add_response(
        content=_sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "crypto_price", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"symbol": "BTC"}'}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
    )
    res = await llm.plan_tools("sys", "price of btc?", TOOL_CATALOG, "respond_directly")
    assert res["name"] == "crypto_price"
    assert res["args"] == {"symbol": "BTC"}


async def test_openai_plan_no_tool_call_is_free_answer(
    openai_env: None,
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    httpx_mock.add_response(
        content=_sse({"choices": [{"delta": {"content": "Here is the answer."}}]})
    )
    res = await llm.plan_tools("sys", "hello", TOOL_CATALOG, "respond_directly")
    assert res["name"] == ""
    assert "answer" in res["text"].lower()


async def test_openai_plan_bad_arguments_json_degrades(
    openai_env: None,
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    httpx_mock.add_response(
        content=_sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"name": "crypto_price", "arguments": "NOT JSON"},
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    res = await llm.plan_tools("sys", "x", TOOL_CATALOG, "respond_directly")
    assert res["name"] == "crypto_price"
    assert res["args"] == {}


# ---- reasoning models -------------------------------------------------------
# A model with no separate reasoning field streams its chain of thought inline as
# a <think> block. Leaking that into an answer would put the model's private
# deliberation in front of a Discord user, so it is stripped in one place.


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<think>2+2 is 4</think>\n\nFour", "Four"),
        # Multi-line and punctuation-heavy thinking, which a lazy regex would eat.
        ("<think>line one\nline two\n</think>Paris.", "Paris."),
        # No block at all: a non-reasoning model must pass through untouched.
        ("plain answer", "plain answer"),
        # Answer inside the block. Returning "" here would read as a failed call,
        # so the block's own text is handed back instead.
        ("<think>reasoning then Four</think>\n\n", "reasoning then Four"),
        # Cut short by max_tokens, so the block never closes.
        ("<think>still thinking about it", "still thinking about it"),
    ],
)
def test_visible_text_drops_the_reasoning_block(content: str, expected: str) -> None:
    assert llm._visible_text(content) == expected


def test_visible_text_keeps_a_think_block_that_is_only_mentioned() -> None:
    # A bare mention is not a block, so nothing should be removed.
    assert llm._visible_text("use </think> carefully") == "use </think> carefully"


async def test_openai_chat_strips_streamed_reasoning(
    openai_env: None,
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    # The tags arrive split across deltas, so stripping has to happen after the
    # stream is reassembled rather than per chunk.
    httpx_mock.add_response(
        content=_sse(
            {"choices": [{"delta": {"content": "<thi"}}]},
            {"choices": [{"delta": {"content": "nk>weighing it up</thi"}}]},
            {"choices": [{"delta": {"content": "nk>\n\nBuy BTC."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )
    assert await llm.chat("hi", max_tokens=10) == "Buy BTC."


async def test_openai_plan_free_answer_has_no_reasoning_in_it(
    openai_env: None,
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    httpx_mock.add_response(
        content=_sse(
            {"choices": [{"delta": {"content": "<think>no tool needed</think>Hello to you too."}}]}
        )
    )
    res = await llm.plan_tools("sys", "hello", TOOL_CATALOG, "respond_directly")
    assert res["name"] == ""
    assert res["text"] == "Hello to you too."
