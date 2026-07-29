"""Tests for the agent decision + spend-governance layer.

The Claude planning call (`planner._plan`) is monkeypatched so the budget logic
is exercised deterministically and offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import planner
from src.agent.tools import get_tool
from src.domain.models import PaymentRecord
from src.payments.store import PaymentStore


def _patch_plan(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def fake(prompt: str, budget: int, catalog: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(planner, "_plan", fake)


async def test_decide_pays_when_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(
        monkeypatch, {"tool": "crypto_price", "args": {"symbol": "BTC"}, "reason": "need price"}
    )
    d = await planner.decide("price of btc?", budget_remaining_atomic=50_000)
    assert d.action == "pay"
    assert d.tool is not None and d.tool.name == "crypto_price"
    assert d.est_cost_atomic == 1000
    assert d.args == {"symbol": "BTC"}


async def test_decide_declines_when_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # deep_answer costs $0.01 (10_000); only $0.005 left.
    _patch_plan(monkeypatch, {"tool": "deep_answer", "args": {"prompt": "essay"}, "reason": "deep"})
    d = await planner.decide("write an essay", budget_remaining_atomic=5_000)
    assert d.action == "decline"
    assert d.est_cost_atomic == 10_000
    assert "budget" in d.reason.lower()


async def test_decide_answers_free_when_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, {"tool": None, "reason": "just chit-chat"})
    d = await planner.decide("hello there", budget_remaining_atomic=50_000)
    assert d.action == "answer_free"
    assert d.tool is None


async def test_decide_unknown_tool_falls_back_to_free(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, {"tool": "frobnicate", "args": {}, "reason": "?"})
    d = await planner.decide("do a thing", budget_remaining_atomic=50_000)
    assert d.action == "answer_free"


async def test_decide_coerces_args_to_str(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, {"tool": "crypto_price", "args": {"symbol": 123}, "reason": "r"})
    d = await planner.decide("price?", budget_remaining_atomic=50_000)
    assert d.args == {"symbol": "123"}


async def test_decide_exact_budget_boundary_pays(monkeypatch: pytest.MonkeyPatch) -> None:
    # cost == remaining should be allowed (not over budget).
    _patch_plan(monkeypatch, {"tool": "crypto_price", "args": {"symbol": "ETH"}, "reason": "r"})
    d = await planner.decide("eth?", budget_remaining_atomic=1000)
    assert d.action == "pay"


async def test_answer_free_applies_nanopay_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_chat(prompt: str, *, max_tokens: int = 600, system: Any = None) -> str:
        captured["system"] = system
        return "I am the NanoPay agent."

    # answer_free asks llm.available() first, and that reads the environment. Stub
    # it too, otherwise this test passes only on a machine with an API key set.
    monkeypatch.setattr(planner.llm, "available", lambda: True)
    monkeypatch.setattr(planner.llm, "chat", fake_chat)
    out = await planner.answer_free("what are you?")
    assert out == "I am the NanoPay agent."
    assert captured["system"] and "NanoPay" in captured["system"]


async def test_answer_free_says_so_when_no_model_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planner.llm, "available", lambda: False)
    out = await planner.answer_free("what are you?")
    assert "unavailable" in out.lower()


def test_tool_catalog_lookup() -> None:
    assert get_tool("crypto_price") is not None
    assert get_tool("nope") is None


def test_resolve_tool_name_tolerates_provider_transforms() -> None:
    from src.agent.tools import TOOL_CATALOG

    # Exact, camel-cased, and suffixed variants all resolve back to the catalog name.
    assert planner._resolve_tool_name("crypto_price", TOOL_CATALOG) == "crypto_price"
    assert planner._resolve_tool_name("CompatCryptoPrice60fd2a", TOOL_CATALOG) == "crypto_price"
    assert planner._resolve_tool_name("tool_weather_v2", TOOL_CATALOG) == "weather"
    # The respond-directly sentinel maps to None regardless of transform.
    assert planner._resolve_tool_name("respond_directly", TOOL_CATALOG) is None
    assert planner._resolve_tool_name("RespondDirectlyXyz", TOOL_CATALOG) is None


# --- budget accounting in the store --------------------------------------


@pytest.fixture
async def store(tmp_path) -> PaymentStore:  # type: ignore[type-arg]
    s = PaymentStore(str(tmp_path / "budget.db"))
    await s.init()
    return s


async def test_total_spent_counts_only_paid(store: PaymentStore) -> None:
    paid = PaymentRecord(user_id="u1", command_name="price", price_atomic=1000)
    await store.create_payment(paid)
    await store.mark_paid(paid.payment_id, "0xabc", "0xpayer", "BTC = $1")

    pending = PaymentRecord(user_id="u1", command_name="weather", price_atomic=1000)
    await store.create_payment(pending)  # never paid

    other_user = PaymentRecord(user_id="u2", command_name="price", price_atomic=9999)
    await store.create_payment(other_user)
    await store.mark_paid(other_user.payment_id, "0xdef", "0xp", "x")

    assert await store.total_spent_atomic("u1") == 1000
    assert await store.total_spent_atomic("u2") == 9999
    assert await store.total_spent_atomic("nobody") == 0
