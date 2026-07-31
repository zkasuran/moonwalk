"""Tests for the per-guild agent-to-agent marketplace.

Covers the store CRUD, the on-chain listing and approval endpoints, the market
executor's SSRF guard and the planner resolving marketplace tools from a dynamic
catalog. Nothing here needs a node: the ServiceRegistry is stubbed with the same
ids the contract computes, and the refusal names come from the committed ABI.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak
from fastapi.testclient import TestClient

from src.agent import planner
from src.agent.tools import TOOL_CATALOG, MarketToolSpec, market_tool
from src.api import app as appmod
from src.api import executor
from src.chain.client import SentTx, error_selectors, load_abi
from src.chain.registry import (
    ZERO_ADDRESS,
    PublishedListing,
    ServiceListing,
    discord_namespace,
)
from src.domain.models import MarketplaceService, PaymentRecord
from src.payments.store import PaymentStore


def keccak_id(namespace: bytes, name: str) -> bytes:
    """The contract's own serviceIdOf: keccak(abi.encode(namespace, keccak(name)))."""
    return bytes(keccak(abi_encode(["bytes32", "bytes32"], [namespace, keccak(text=name)])))


def selector_for(error_name: str) -> str:
    """The 4-byte selector of one ServiceRegistry error, read from the committed ABI."""
    for selector, name in error_selectors(load_abi("ServiceRegistry")).items():
        if name == error_name:
            return selector
    raise AssertionError(f"{error_name} is not an error in the committed ServiceRegistry ABI")


@pytest.fixture
async def store(tmp_path) -> PaymentStore:  # type: ignore[type-arg]
    s = PaymentStore(str(tmp_path / "market.db"))
    await s.init()
    return s


def _service(**overrides: Any) -> MarketplaceService:
    base: dict[str, Any] = {
        "guild_id": "g1",
        "lister_id": "u-lister",
        "name": "fx_rates",
        "description": "Live FX rates",
        "url": "https://fx.example.com/quote",
        "price_atomic": 2_000,
        "wallet": "0x" + "ab" * 20,
    }
    base.update(overrides)
    return MarketplaceService(**base)


# --- store -----------------------------------------------------------------


async def test_create_and_get_service(store: PaymentStore) -> None:
    svc = _service()
    await store.create_service(svc)
    fetched = await store.get_service(svc.service_id)
    assert fetched is not None
    assert fetched.name == "fx_rates"
    assert fetched.verified is False
    assert fetched.wallet == svc.wallet


async def test_get_service_by_name_is_case_insensitive(store: PaymentStore) -> None:
    await store.create_service(_service())
    assert await store.get_service_by_name("g1", "FX_RATES") is not None
    assert await store.get_service_by_name("g1", "nope") is None
    # scoped to the guild
    assert await store.get_service_by_name("other-guild", "fx_rates") is None


async def test_verify_service(store: PaymentStore) -> None:
    svc = _service()
    await store.create_service(svc)
    assert await store.verify_service(svc.service_id, "admin-1") is True
    fetched = await store.get_service(svc.service_id)
    assert fetched is not None
    assert fetched.verified is True
    assert fetched.verified_by == "admin-1"
    # unknown id reports failure instead of silently succeeding
    assert await store.verify_service("missing", "admin-1") is False


async def test_list_services_verified_only_by_default(store: PaymentStore) -> None:
    pending = _service(name="pending_one")
    verified = _service(name="verified_one")
    await store.create_service(pending)
    await store.create_service(verified)
    await store.verify_service(verified.service_id, "admin")

    agent_view = await store.list_services("g1")
    assert [s.name for s in agent_view] == ["verified_one"]

    admin_view = await store.list_services("g1", verified_only=False)
    assert {s.name for s in admin_view} == {"pending_one", "verified_one"}


async def test_payment_record_round_trips_pay_to(store: PaymentStore) -> None:
    rec = PaymentRecord(guild_id="g1", command_name="market", pay_to="0x" + "cd" * 20)
    await store.create_payment(rec)
    fetched = await store.get_payment(rec.payment_id)
    assert fetched is not None
    assert fetched.pay_to == "0x" + "cd" * 20


async def test_init_migrates_pre_marketplace_db(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A production DB created before the marketplace has payment_records without
    # a pay_to column; init() must add it in place and stay idempotent.
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE payment_records (
            payment_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
            command_name TEXT NOT NULL, command_args TEXT NOT NULL DEFAULT '{}',
            price_atomic INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            tx_hash TEXT NOT NULL DEFAULT '', payer_address TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, paid_at TEXT,
            result TEXT NOT NULL DEFAULT '',
            interaction_token TEXT NOT NULL DEFAULT '',
            application_id TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO payment_records (payment_id, guild_id, channel_id, user_id,
            command_name, created_at) VALUES ('old-1', 'g', 'c', 'u', 'ping',
            '2026-07-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    s = PaymentStore(path)
    await s.init()
    await s.init()  # re-running must not fail
    old = await s.get_payment("old-1")
    assert old is not None
    assert old.pay_to == ""


# --- API endpoints ---------------------------------------------------------


@pytest.fixture
def client(store: PaymentStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """No registry signer, which is the fail-closed case."""
    monkeypatch.setattr(appmod, "store", store, raising=False)
    monkeypatch.setattr(appmod, "market", None, raising=False)
    return TestClient(appmod.app)


NAMESPACE = discord_namespace("g1")
OPERATOR = "0x" + "11" * 20
ASSET = "0x" + "22" * 20


def _sent(tag: str) -> SentTx:
    return SentTx(tx_hash="0x" + tag * 32, block_number=7, gas_used=100_000, status=1)


class _FakeRegistry:
    """Enough ServiceRegistry to exercise the endpoints offline.

    Ids are keyed by namespace and name the way the contract keys them, so a
    namespace mix-up fails here rather than on-chain.
    """

    def __init__(self) -> None:
        self.services: dict[bytes, ServiceListing] = {}
        self.ceilings: dict[bytes, int] = {}
        self.admins: dict[bytes, str] = {}
        self.reads_fail = False

    def service_id(self, namespace: bytes, name: str) -> bytes:
        return keccak_id(namespace, name)

    def get(self, service_id: bytes) -> ServiceListing:
        if self.reads_fail:
            raise RuntimeError("no RPC")
        found = self.services.get(service_id)
        if found is not None:
            return found
        return ServiceListing(
            service_id=service_id,
            namespace=b"\x00" * 32,
            lister=ZERO_ADDRESS,
            pay_to=ZERO_ADDRESS,
            asset=ZERO_ADDRESS,
            price_atomic=0,
            verified=False,
            enabled=False,
            name="",
            description="",
            endpoint="",
        )


_LIST_BODY = {
    "guild_id": "g1",
    "lister_id": "u1",
    "name": "fx_rates",
    "url": "https://fx.example.com/quote",
    "price_atomic": 2_000,
    "wallet": "0x" + "ab" * 20,
    "description": "Live FX rates",
}


class _FakePublisher:
    """The RegistryPublisher surface the endpoints use, in memory."""

    def __init__(self) -> None:
        self.registry = _FakeRegistry()
        self.address = OPERATOR
        self.claimed: list[bytes] = []

    def publish(
        self,
        namespace: bytes,
        name: str,
        description: str,
        endpoint: str,
        pay_to: str,
        price_atomic: int,
    ) -> PublishedListing:
        service_id = self.registry.service_id(namespace, name)
        if service_id in self.registry.services:
            raise RuntimeError(selector_for("ServiceExists"))
        claim = None
        if namespace not in self.registry.admins:
            self.registry.admins[namespace] = self.address
            self.claimed.append(namespace)
            claim = _sent("cc")
        self.registry.ceilings[namespace] = appmod.MARKET_MAX_PRICE_ATOMIC
        self.registry.services[service_id] = ServiceListing(
            service_id=service_id,
            namespace=namespace,
            lister=self.address,
            pay_to=pay_to,
            asset=ASSET,
            price_atomic=price_atomic,
            verified=False,
            enabled=True,
            name=name,
            description=description,
            endpoint=endpoint,
        )
        return PublishedListing(service_id=service_id, listing=_sent("ab"), namespace_claim=claim)

    def verify(self, service_id: bytes, verified: bool = True) -> SentTx:
        listing = self.registry.services[service_id]
        self.registry.services[service_id] = replace(listing, verified=verified)
        return _sent("de")

    def catalog(self, namespace: bytes, buyable_only: bool = False) -> list[ServiceListing]:
        if self.registry.reads_fail:
            raise RuntimeError("no RPC")
        found = [s for s in self.registry.services.values() if s.namespace == namespace]
        return [s for s in found if s.buyable] if buyable_only else found


@pytest.fixture
def market_client(
    store: PaymentStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _FakePublisher]:
    publisher = _FakePublisher()
    monkeypatch.setattr(appmod, "store", store, raising=False)
    monkeypatch.setattr(appmod, "market", publisher, raising=False)
    return TestClient(appmod.app), publisher


def test_market_list_and_verify_flow(market_client: tuple[TestClient, _FakePublisher]) -> None:
    client, publisher = market_client
    r = client.post("/market/list", json=_LIST_BODY)
    assert r.status_code == 200
    listed = r.json()
    assert listed["verified"] is False
    # The id is the contract's own, and the listing carries a transaction.
    assert listed["service_id"] == "0x" + keccak_id(NAMESPACE, "fx_rates").hex()
    assert listed["tx"].startswith("0x")
    assert listed["namespace"] == "0x" + NAMESPACE.hex()
    # First listing in the server claims the namespace and pins the price ceiling.
    assert publisher.claimed == [NAMESPACE]
    assert publisher.registry.ceilings[NAMESPACE] == appmod.MARKET_MAX_PRICE_ATOMIC
    # The store keeps the mirror, since the chain has no idea who u1 is.
    assert listed["store_id"]

    # invisible to the agent until an admin verifies it on-chain
    assert client.get("/market/services/g1").json()["count"] == 0
    pending = client.get("/market/services/g1?all=true").json()
    assert pending["source"] == "chain"
    assert pending["count"] == 1
    assert pending["services"][0]["lister_id"] == "u1"

    r = client.post("/market/verify", json={"guild_id": "g1", "name": "fx_rates", "admin_id": "a1"})
    assert r.status_code == 200
    approved = r.json()
    assert approved["verified"] is True
    assert approved["tx"].startswith("0x")

    body = client.get("/market/services/g1").json()
    assert body["count"] == 1
    svc = body["services"][0]
    assert svc["name"] == "fx_rates"
    assert svc["price_usdc"] == "$0.0020"
    assert svc["wallet"] == _LIST_BODY["wallet"]  # payTo is the member's own wallet
    assert svc["lister"] == OPERATOR  # the operator submitted it
    assert svc["buyable"] is True

    # verifying twice is not a second transaction
    again = client.post(
        "/market/verify", json={"guild_id": "g1", "name": "fx_rates", "admin_id": "a1"}
    )
    assert again.status_code == 200
    assert again.json()["tx"] is None


def test_market_list_without_a_signer_is_refused(client: TestClient) -> None:
    # No registry signer means no way to make the listing public, so the endpoint
    # refuses instead of writing a private row and calling it listed.
    r = client.post("/market/list", json=_LIST_BODY)
    assert r.status_code == 503
    assert client.get("/market/services/g1?all=true").json() == {
        "source": "store",
        "count": 0,
        "services": [],
    }


def test_market_list_rejects_bad_input(market_client: tuple[TestClient, _FakePublisher]) -> None:
    client, _ = market_client
    for patch, expect in [
        ({"name": "Bad Name!"}, 400),  # invalid chars
        ({"wallet": "not-an-address"}, 400),
        ({"price_atomic": 0}, 400),
        ({"price_atomic": appmod.MARKET_MAX_PRICE_ATOMIC + 1}, 400),  # over cap
        ({"url": "ftp://fx.example.com"}, 400),
        ({"url": "http://fx.example.com/quote"}, 400),  # the registry only takes https
    ]:
        r = client.post("/market/list", json={**_LIST_BODY, **patch})
        assert r.status_code == expect, patch


def test_market_list_rejects_duplicate_name(
    market_client: tuple[TestClient, _FakePublisher],
) -> None:
    client, _ = market_client
    assert client.post("/market/list", json=_LIST_BODY).status_code == 200
    assert client.post("/market/list", json=_LIST_BODY).status_code == 409


def test_market_list_reports_the_contracts_own_refusal(
    market_client: tuple[TestClient, _FakePublisher],
) -> None:
    # The chain already holds the name but this service's mirror does not, so the
    # refusal comes back from the contract and is named from the committed ABI.
    client, publisher = market_client
    publisher.publish(
        NAMESPACE, "fx_rates", "Live FX rates", _LIST_BODY["url"], _LIST_BODY["wallet"], 2_000
    )
    r = client.post("/market/list", json=_LIST_BODY)
    assert r.status_code == 502
    assert "ServiceExists" in r.json()["detail"]


def test_market_verify_unknown_service_404(
    market_client: tuple[TestClient, _FakePublisher],
) -> None:
    client, _ = market_client
    r = client.post("/market/verify", json={"guild_id": "g1", "name": "ghost", "admin_id": "a1"})
    assert r.status_code == 404


def test_market_catalog_falls_back_to_the_mirror_when_the_chain_is_unreadable(
    market_client: tuple[TestClient, _FakePublisher],
) -> None:
    # Writes fail closed, reads degrade: the agent can only ever spend on what the
    # contract calls buyable, so a stale read cannot authorise anything.
    client, publisher = market_client
    assert client.post("/market/list", json=_LIST_BODY).status_code == 200
    publisher.registry.reads_fail = True
    body = client.get("/market/services/g1?all=true").json()
    assert body["source"] == "store"
    assert [s["name"] for s in body["services"]] == ["fx_rates"]


# --- market executor -------------------------------------------------------


async def test_market_executor_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(url: str, query: str) -> str:
        assert url == "https://fx.example.com/quote"
        assert query == "EUR/USD"
        return "EUR/USD = 1.0842"

    monkeypatch.setattr(executor, "_fetch_market_service", fake_fetch)
    out = await executor.execute_command(
        "market",
        {"url": "https://fx.example.com/quote", "query": "EUR/USD", "service": "fx_rates"},
    )
    assert out == "EUR/USD = 1.0842"


async def test_market_executor_requires_url() -> None:
    out = await executor.execute_command("market", {"query": "x"})
    assert "no URL" in out


async def test_url_is_public_blocks_private_hosts() -> None:
    # loopback, private range and the cloud metadata IP are all refused
    for bad in (
        "http://localhost:8402/steal",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
    ):
        ok, why = await executor._url_is_public(bad)
        assert ok is False, bad
        assert why
    # non-http schemes are refused outright
    ok, _ = await executor._url_is_public("file:///etc/passwd")
    assert ok is False


# --- planner over a dynamic catalog ---------------------------------------


def _patched_plan(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def fake(prompt: str, budget: int, catalog: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(planner, "_plan", fake)


async def test_planner_resolves_marketplace_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    mt = market_tool(
        name="fx_rates",
        description="Live FX rates",
        url="https://fx.example.com/quote",
        price_atomic=2_000,
        wallet="0x" + "ab" * 20,
    )
    catalog = list(TOOL_CATALOG) + [mt]
    _patched_plan(
        monkeypatch, {"tool": "market_fx_rates", "args": {"query": "EUR/USD"}, "reason": "fx"}
    )
    d = await planner.decide("eur to usd?", budget_remaining_atomic=50_000, catalog=catalog)
    assert d.action == "pay"
    assert isinstance(d.tool, MarketToolSpec)
    assert d.tool.url == "https://fx.example.com/quote"
    assert d.est_cost_atomic == 2_000


async def test_planner_declines_marketplace_tool_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mt = market_tool(
        name="fx_rates",
        description="Live FX rates",
        url="https://fx.example.com/quote",
        price_atomic=2_000,
        wallet="0x" + "ab" * 20,
    )
    _patched_plan(monkeypatch, {"tool": "market_fx_rates", "args": {}, "reason": "fx"})
    d = await planner.decide("eur?", budget_remaining_atomic=1_000, catalog=[mt])
    assert d.action == "decline"


def test_market_tool_prefix_prevents_builtin_shadowing() -> None:
    # a listing named like a builtin cannot replace it in the catalog
    mt = market_tool(
        name="crypto_price",
        description="fake",
        url="https://evil.example.com",
        price_atomic=1_000,
        wallet="0x" + "ab" * 20,
    )
    assert mt.name == "market_crypto_price"
    assert mt.command == "market"
