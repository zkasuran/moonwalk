"""ChannelRail: the service side of the channel, tested offline.

The rail is the piece that decides whether a call happens, so its refusals matter
as much as its accepts. Everything here runs against fakes for the chain, since
what is being tested is the rail's logic rather than web3.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from src.chain.channel import Voucher, voucher_to_dict
from src.chain.client import SentTx
from src.chain.guard import Cap
from src.chain.subjects import discord_subject
from src.payments.channel_rail import ChannelRail
from src.payments.store import PaymentStore

CHANNEL_ID = bytes.fromhex("ac" * 32)
PAYER = "0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4"
STRANGER = "0x000000000000000000000000000000000000dEaD"
GUILD = "1416577435369214084"
USER = "900000000000000001"
PRICE = 1_000


class FakeChain:
    """Stands in for ChannelClient. Records what it was asked to redeem."""

    def __init__(
        self, outstanding: int = 500_000, redeemed: dict[bytes, int] | None = None
    ) -> None:
        self._outstanding = outstanding
        self._redeemed = redeemed or {}
        self.signer = PAYER
        self.redeems: list[tuple[list[Voucher], list[bytes]]] = []
        self.status = 1

    def recover_voucher(self, voucher: Voucher, signature: bytes) -> str:
        return self.signer

    def outstanding(self, channel_id: bytes) -> int:
        return self._outstanding

    def subject_redeemed(self, channel_id: bytes, subject: bytes) -> int:
        return self._redeemed.get(subject, 0)

    def state(self, channel_id: bytes) -> Any:
        raise AssertionError("snapshot is not under test here")

    def redeem(
        self, service: Any, channel_id: bytes, vouchers: list[Voucher], signatures: list[bytes]
    ) -> SentTx:
        self.redeems.append((vouchers, signatures))
        return SentTx(
            tx_hash="0x" + "11" * 32,
            block_number=1,
            gas_used=262_639,
            status=self.status,
            effective_gas_price=25_000_000_000,
        )


class FakeGuard:
    def __init__(self, remaining: int = 50_000) -> None:
        self._remaining = remaining
        self.caps: list[tuple[bytes, int, int]] = []

    def remaining(self, channel_id: bytes, subject: bytes) -> int:
        return self._remaining

    def cap_of(self, channel_id: bytes, subject: bytes) -> Cap:
        for booked, limit, window in self.caps:
            if booked == subject:
                return Cap(limit_atomic=limit, window_seconds=window, configured=True)
        return Cap(limit_atomic=self._remaining, window_seconds=86_400, configured=True)

    def set_subject_cap(
        self, owner: Any, channel_id: bytes, subject: bytes, limit: int, window: int
    ) -> SentTx:
        self.caps.append((subject, limit, window))
        return SentTx(tx_hash="0x" + "22" * 32, block_number=2, gas_used=50_000, status=1)


async def build_rail(
    outstanding: int = 500_000,
    cap_remaining: int = 50_000,
    redeemed: dict[bytes, int] | None = None,
    threshold: int = 20_000,
) -> tuple[ChannelRail, FakeChain, FakeGuard, PaymentStore]:
    store = PaymentStore(str(Path(tempfile.mkdtemp()) / "rail.db"))
    await store.init()
    chain = FakeChain(outstanding=outstanding, redeemed=redeemed)
    guard = FakeGuard(remaining=cap_remaining)
    rail = ChannelRail(
        store=store,
        chain=chain,  # type: ignore[arg-type]
        guard=guard,  # type: ignore[arg-type]
        service=object(),  # type: ignore[arg-type]
        channel_id=CHANNEL_ID,
        payer=PAYER,
        threshold_atomic=threshold,
        cache_ttl_seconds=0.0,
    )
    return rail, chain, guard, store


def voucher_for(cumulative: int, subject: bytes | None = None, ttl: int = 3600) -> Voucher:
    return Voucher(
        channel_id=CHANNEL_ID,
        subject=subject if subject is not None else discord_subject(GUILD, USER),
        cumulative=cumulative,
        valid_before=int(time.time()) + ttl,
    )


async def test_quote_asks_for_the_next_cumulative() -> None:
    rail, _chain, _guard, _store = await build_rail()
    quote = await rail.quote(GUILD, USER, PRICE)
    assert quote.cumulative == PRICE, "a first call starts at the price"
    assert quote.subject == "0x" + discord_subject(GUILD, USER).hex()
    assert quote.cap_remaining_atomic == 50_000
    assert quote.valid_before > int(time.time())

    await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)
    again = await rail.quote(GUILD, USER, PRICE)
    assert again.cumulative == 2 * PRICE, "the ledger, not the payer, tracks the total"
    assert again.cap_remaining_atomic == 49_000, "unsettled spend is taken off the cap"


async def test_record_accepts_a_good_voucher_and_meters_it() -> None:
    rail, _chain, _guard, store = await build_rail()
    outcome = await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)
    assert outcome.accepted
    assert outcome.cumulative_atomic == PRICE
    assert outcome.calls == 1
    assert outcome.unsettled_atomic == PRICE
    assert outcome.cap_remaining_atomic == 49_000

    meter = await store.get_meter(rail.channel_id_hex, outcome.subject)
    assert meter is not None
    assert json.loads(meter.voucher_json)["cumulative"] == PRICE, "the voucher is kept for redeem"


@pytest.mark.parametrize(
    "voucher,reason_contains",
    [
        (
            Voucher(
                channel_id=bytes.fromhex("bb" * 32),
                subject=discord_subject(GUILD, USER),
                cumulative=PRICE,
                valid_before=int(time.time()) + 3600,
            ),
            "another channel",
        ),
        (
            Voucher(
                channel_id=CHANNEL_ID,
                subject=discord_subject(GUILD, "someone-else"),
                cumulative=PRICE,
                valid_before=int(time.time()) + 3600,
            ),
            "subject does not match",
        ),
        (
            Voucher(
                channel_id=CHANNEL_ID,
                subject=discord_subject(GUILD, USER),
                cumulative=PRICE,
                valid_before=int(time.time()) - 1,
            ),
            "expired",
        ),
        (
            Voucher(
                channel_id=CHANNEL_ID,
                subject=discord_subject(GUILD, USER),
                cumulative=PRICE * 5,
                valid_before=int(time.time()) + 3600,
            ),
            "cumulative should be",
        ),
    ],
)
async def test_record_refuses_a_voucher_it_could_not_redeem(
    voucher: Voucher, reason_contains: str
) -> None:
    rail, _chain, _guard, _store = await build_rail()
    outcome = await rail.record(GUILD, USER, PRICE, voucher, b"\x00" * 65)
    assert not outcome.accepted
    assert reason_contains in outcome.reason


async def test_record_refuses_a_voucher_the_payer_did_not_sign() -> None:
    rail, chain, _guard, _store = await build_rail()
    chain.signer = STRANGER
    outcome = await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)
    assert not outcome.accepted
    assert "not the channel payer" in outcome.reason


async def test_record_refuses_once_the_on_chain_cap_is_spent() -> None:
    rail, _chain, _guard, _store = await build_rail(cap_remaining=PRICE)
    first = await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)
    assert first.accepted
    second = await rail.record(GUILD, USER, PRICE, voucher_for(2 * PRICE), b"\x00" * 65)
    assert not second.accepted
    assert "cap leaves 0 atomic" in second.reason, second.reason


async def test_record_refuses_what_the_deposit_cannot_cover() -> None:
    rail, _chain, _guard, _store = await build_rail(outstanding=PRICE)
    assert (await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)).accepted
    outcome = await rail.record(GUILD, USER, PRICE, voucher_for(2 * PRICE), b"\x00" * 65)
    assert not outcome.accepted
    assert "top it up" in outcome.reason


async def test_settle_waits_for_the_threshold_then_collects_everything() -> None:
    rail, chain, _guard, store = await build_rail(threshold=3 * PRICE)
    for i in range(1, 3):
        await rail.record(GUILD, USER, PRICE, voucher_for(i * PRICE), b"\x00" * 65)
    assert await rail.settle() is None, "under the threshold, nothing is collected"
    assert chain.redeems == []

    other = discord_subject(GUILD, "900000000000000002")
    await rail.record(
        GUILD, "900000000000000002", PRICE, voucher_for(PRICE, subject=other), b"\x00" * 65
    )
    settlement = await rail.settle()
    assert settlement is not None
    assert settlement.total_atomic == 3 * PRICE
    assert settlement.subject_count == 2, "one voucher per subject, not per call"
    assert settlement.calls == 3
    assert settlement.gas_fee_atomic == 262_639 * 25_000_000_000 // 10**12

    vouchers, _sigs = chain.redeems[0]
    assert {v.cumulative for v in vouchers} == {2 * PRICE, PRICE}
    assert await rail.pending_total() == 0, "the ledger follows the chain after a redeem"
    assert len(await store.recent_channel_settlements()) == 1


async def test_settle_raises_when_the_redeem_reverts() -> None:
    rail, chain, _guard, _store = await build_rail(threshold=0)
    await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)
    chain.status = 0
    with pytest.raises(RuntimeError, match="redeem failed"):
        await rail.settle(force=True)
    assert await rail.pending_total() == PRICE, "a failed redeem leaves the ledger alone"


async def test_a_fresh_service_picks_up_where_the_chain_left_off() -> None:
    """The property that makes the service operable: lose the database and the
    contract's own record of what it paid out becomes the floor."""
    subject = discord_subject(GUILD, USER)
    rail, _chain, _guard, store = await build_rail(redeemed={subject: 4_000})

    quote = await rail.quote(GUILD, USER, PRICE)
    assert quote.cumulative == 5_000, "not 1000, the chain already settled 4000"

    stale = await rail.record(GUILD, USER, PRICE, voucher_for(PRICE), b"\x00" * 65)
    assert not stale.accepted, "a voucher built from a zeroed ledger is refused"

    good = await rail.record(GUILD, USER, PRICE, voucher_for(5_000), b"\x00" * 65)
    assert good.accepted
    assert good.unsettled_atomic == PRICE, "only the new call is owed"

    meter = await store.get_meter(rail.channel_id_hex, "0x" + subject.hex())
    assert meter is not None
    assert meter.settled_atomic == 4_000, "the correction was written back"


async def test_set_cap_goes_to_the_contract_and_clears_the_cache() -> None:
    rail, _chain, guard, _store = await build_rail()
    sent = await rail.set_cap(GUILD, USER, 25_000, 86_400)
    assert sent.ok
    assert guard.caps == [(discord_subject(GUILD, USER), 25_000, 86_400)]

    cap = await rail.cap_of(GUILD, USER)
    assert cap["subject"] == "0x" + discord_subject(GUILD, USER).hex()
    assert cap["callsMetered"] == 0


async def test_voucher_wire_form_survives_the_store() -> None:
    voucher = voucher_for(7_000)
    payload = voucher_to_dict(voucher, b"\xab" * 65)
    assert payload["channelId"].startswith("0x")
    assert payload["cumulative"] == 7_000
    assert len(payload["signature"]) == 2 + 130
