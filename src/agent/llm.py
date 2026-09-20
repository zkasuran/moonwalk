"""LLM provider layer: Anthropic or any OpenAI-compatible endpoint.

The agent's reasoning is free (it runs on the operator's model). This module lets
that model be Anthropic or any OpenAI-compatible API (a gateway or self-hosted
endpoint) chosen by env, so the planner and executor do not care which is behind
it. Two calls are exposed:

  chat(prompt)   -> a plain text answer (used for free answers and composing)
  plan_tools(..) -> the model picks one tool or none (the agentic decision)

`plan_tools` returns a uniform dict {"name": <raw tool name or "">, "args": {...},
"text": <assistant text if it answered instead of calling a tool>}. The caller
resolves the raw name against the catalog, so provider-side name transforms and
the respond-directly sentinel stay handled in one place (the planner).
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import httpx

from ..payments import config
from .tools import ToolSpec

_HTTP_TIMEOUT = 60.0  # a streaming reasoning model can pause before its first token

# A reasoning model that has no separate field for its chain of thought wraps it
# in <think>...</think> inside the ordinary content stream. MiniMax M3 does this.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def provider() -> str:
    """Which backend to use: a validated explicit override, else auto-detect.

    An explicit LLM_PROVIDER only wins if that provider's credentials are present,
    so a forced-but-misconfigured value (e.g. openai with no base URL) resolves to
    'none' and callers degrade cleanly instead of building a broken request. An
    unrecognised value falls through to auto-detect rather than being trusted.
    """
    forced = config.LLM_PROVIDER.strip().lower()
    if forced == "openai":
        return "openai" if (config.OPENAI_API_KEY and config.OPENAI_BASE_URL) else "none"
    if forced == "anthropic":
        return "anthropic" if config.ANTHROPIC_API_KEY else "none"
    if config.OPENAI_API_KEY and config.OPENAI_BASE_URL:
        return "openai"
    if config.ANTHROPIC_API_KEY:
        return "anthropic"
    return "none"


def available() -> bool:
    return provider() != "none"


async def chat(prompt: str, *, max_tokens: int = 600, system: str | None = None) -> str:
    p = provider()
    if p == "openai":
        return await _openai_chat(prompt, max_tokens=max_tokens, system=system)
    if p == "anthropic":
        return await _anthropic_chat(prompt, max_tokens=max_tokens, system=system)
    return ""


async def plan_tools(
    system: str,
    prompt: str,
    catalog: list[ToolSpec],
    respond_directly_name: str,
    max_tokens: int = 400,
) -> dict[str, Any]:
    p = provider()
    if p == "openai":
        return await _openai_plan(system, prompt, catalog, respond_directly_name, max_tokens)
    if p == "anthropic":
        return await _anthropic_plan(system, prompt, catalog, respond_directly_name, max_tokens)
    return {"name": "", "args": {}, "text": ""}


# ============================================================================
# OpenAI-compatible (chat-completions protocol)
# ============================================================================


async def _openai_post(body: dict[str, Any]) -> dict[str, Any]:
    """POST to /chat/completions and return one non-streaming-shaped response.

    This always sends stream=true and reassembles the SSE deltas back into the
    ``{"choices": [{"message": ...}]}`` shape the parsers below expect, so callers
    stay unaware that the wire format is a stream. Streaming is what a reasoning
    model wants anyway, since it can think for seconds before its first token, and
    some gateways serve nothing else.
    """
    url = config.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {**body, "stream": True}
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}  # keyed by the streamed call index
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        async with client.stream(
            "POST",
            url,
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()  # load the body so raise_for_status can report it
                resp.raise_for_status()
            async for line in resp.aiter_lines():
                data = _sse_payload(line)
                if data is None:
                    continue
                if data == "[DONE]":
                    break
                try:
                    choices = json.loads(data).get("choices") or []
                except ValueError:
                    continue
                if not choices:
                    continue  # e.g. the trailing usage-only chunk
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                for frag in delta.get("tool_calls") or []:
                    _merge_tool_call(tool_calls, frag)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": _visible_text("".join(content_parts)),
    }
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"choices": [{"message": message}]}


def _visible_text(content: str) -> str:
    """Return what the model meant to say, without its chain of thought.

    A reasoning model with no separate reasoning field streams its thinking inline
    as ``<think>...</think>`` and the answer sits after it, so the block is dropped.
    Two degraded shapes are handled rather than returned as an empty answer, which
    would read to a caller as a failed call:

    * a response cut short by ``max_tokens`` can leave the block unterminated;
    * the model sometimes closes the block after the answer rather than before it,
      leaving nothing outside. Then its own text is the best answer available.
    """
    visible = _THINK_BLOCK.sub("", content)
    head, unterminated, _ = visible.partition("<think>")
    visible = (head if unterminated else visible).strip()
    if visible:
        return visible
    inner = content.replace("<think>", " ").replace("</think>", " ").strip()
    return inner or content.strip()


def _sse_payload(line: str) -> str | None:
    """Return the payload of an SSE ``data:`` line, or None for anything else."""
    if not line.startswith("data:"):
        return None
    return line[len("data:") :].strip()


def _merge_tool_call(acc: dict[int, dict[str, Any]], frag: dict[str, Any]) -> None:
    """Fold one streamed tool_call fragment into the accumulator keyed by call index.

    The name and id arrive whole in the opening fragment; the JSON arguments stream in
    pieces across later fragments. Concatenation is safe for all of them.
    """
    idx = int(frag.get("index") or 0)
    call = acc.setdefault(
        idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    )
    if frag.get("id"):
        call["id"] = frag["id"]
    if frag.get("type"):
        call["type"] = frag["type"]
    fn = frag.get("function") or {}
    if fn.get("name"):
        call["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        call["function"]["arguments"] += fn["arguments"]


def _first_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") or []
    if not choices:
        return {}
    return cast("dict[str, Any]", choices[0].get("message") or {})


async def _openai_chat(prompt: str, *, max_tokens: int, system: str | None) -> str:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = await _openai_post(
        {"model": config.OPENAI_MODEL, "messages": messages, "max_tokens": max_tokens}
    )
    content = _first_message(data).get("content") or ""
    return str(content).strip()


def _openai_tools(catalog: list[ToolSpec], respond_directly_name: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for t in catalog:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": f"{t.description} Costs {t.price_display} USDC per call.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            t.arg_name: {"type": "string", "description": t.arg_description}
                        },
                        "required": [t.arg_name],
                    },
                },
            }
        )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": respond_directly_name,
                "description": "Answer the user directly for free, without buying any tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Why no paid tool is needed."}
                    },
                    "required": ["reason"],
                },
            },
        }
    )
    return tools


async def _openai_plan(
    system: str,
    prompt: str,
    catalog: list[ToolSpec],
    respond_directly_name: str,
    max_tokens: int,
) -> dict[str, Any]:
    data = await _openai_post(
        {
            "model": config.OPENAI_MODEL,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "tools": _openai_tools(catalog, respond_directly_name),
            "tool_choice": "auto",
        }
    )
    message = _first_message(data)
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        fn = cast("dict[str, Any]", tool_calls[0].get("function") or {})
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (ValueError, TypeError):
            parsed = {}
        args = parsed if isinstance(parsed, dict) else {}
        return {"name": str(fn.get("name") or ""), "args": args, "text": ""}
    # No tool call: the model answered directly, treat as respond-directly.
    return {"name": "", "args": {}, "text": str(message.get("content") or "").strip()}


# ============================================================================
# Anthropic (messages protocol with tool-use)
# ============================================================================


async def _anthropic_chat(prompt: str, *, max_tokens: int, system: str | None) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    kwargs: dict[str, Any] = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    msg = await client.messages.create(**kwargs)
    parts = [b.text for b in msg.content if b.type == "text"]
    return "\n".join(parts).strip()


def _anthropic_tools(catalog: list[ToolSpec], respond_directly_name: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for t in catalog:
        tools.append(
            {
                "name": t.name,
                "description": f"{t.description} Costs {t.price_display} USDC per call.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        t.arg_name: {"type": "string", "description": t.arg_description}
                    },
                    "required": [t.arg_name],
                },
            }
        )
    tools.append(
        {
            "name": respond_directly_name,
            "description": "Answer the user directly for free, without buying any tool.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why no paid tool is needed."}
                },
                "required": ["reason"],
            },
        }
    )
    return tools


async def _anthropic_plan(
    system: str,
    prompt: str,
    catalog: list[ToolSpec],
    respond_directly_name: str,
    max_tokens: int,
) -> dict[str, Any]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        tools=cast("Any", _anthropic_tools(catalog, respond_directly_name)),
        tool_choice=cast("Any", {"type": "any"}),
        messages=[{"role": "user", "content": prompt}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            raw = block.input
            args = raw if isinstance(raw, dict) else {}
            return {"name": block.name, "args": args, "text": ""}
    return {"name": "", "args": {}, "text": ""}
