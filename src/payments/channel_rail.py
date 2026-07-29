"""The service side of the nanopayment channel.

The agent signs a voucher per call. This is what the service does with it: check
the voucher is one it could actually redeem, remember it, and once enough has
piled up, collect the lot in a single transaction.

Two rules keep this honest. The service never delivers on a voucher it could not
redeem, so it checks the signature, the cumulative and the on-chain cap before the
work happens. And the caps it checks are the contract's, not its own, so refusing
a call and failing to redeem it can never disagree.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from eth_account.signers.local import LocalAccount

from ..chain import config as chain_config
from ..chain.channel import ChannelClient, Voucher, voucher_from_dict, voucher_to_dict
from ..chain.client import ArcClient, SentTx
from ..chain.guard import GuardClient
from ..chain.subjects import discord_subject
from ..domain.models import ChannelSettlement
from .store import PaymentStore

logger = logging.getLogger("moonwalk.channel")


@dataclass(frozen=True)
class ChannelQuote:
    """What the payer must sign for the next call."""

    channel_id: str
    subject: str
    cumulative: int
    valid_before: int
    price_atomic: int
    cap_remaining_atomic: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "channelId": self.channel_id,
            "subject": self.subject,
            "cumulative": self.cumulative,
            "validBefore": self.valid_before,
            "priceAtomic": self.price_atomic,
            "capRemainingAtomic": self.cap_remaining_atomic,
        }


@dataclass(frozen=True)
class MeterOutcome:
    accepted: bool
    reason: str
    subject: str = ""
    cumulative_atomic: int = 0
    calls: int = 0
    unsettled_atomic: int = 0
    cap_remaining_atomic: int = 0


def build_rail(store: PaymentStore) -> ChannelRail | None:
    """Wire the rail from the environment, or return None if it is not ready.

    Needs the service key, since the service is what submits a redeem, the payer's
    address, and a channel that already exists on-chain. Missing any of those the
    app still runs the per-call x402 rail, it just cannot batch.
    """
    from eth_utils.crypto import keccak

    from . import config as pay_config

    if not pay_config.FACILITATOR_PRIVATE_KEY:
        return None
    payer = pay_config.CHANNEL_PAYER_ADDRESS
    if not payer and pay_config.AGENT_PRIVATE_KEY:
        payer = ArcClient.account(pay_config.AGENT_PRIVATE_KEY).address
    if not payer:
        return None

    try:
        client = ArcClient()
        service = ArcClient.account(pay_config.FACILITATOR_PRIVATE_KEY)
        service_address = pay_config.SELLER_WALLET_ADDRESS or service.address
        chain = ChannelClient(client)
        salt = keccak(text=pay_config.CHANNEL_SALT)
        channel_id = ChannelClient.channel_id_local(payer, service_address, salt)
        state = chain.state(channel_id)
        if state.payer == "0x0000000000000000000000000000000000000000":
            logger.info(
                "channel %s not open yet, run scripts/open_channel.py to enable the batch rail",
                "0x" + channel_id.hex(),
            )
            return None
        if state.settled:
            logger.info(
                "channel %s is closed, open a new one with a fresh salt", "0x" + channel_id.hex()
            )
            return None
        return ChannelRail(
            store=store,
            chain=chain,
            guard=GuardClient(client),
            service=service,
            channel_id=channel_id,
            payer=payer,
            threshold_atomic=pay_config.REDEEM_THRESHOLD_ATOMIC,
        )
    except Exception as exc:  # noqa: BLE001 - never let a chain hiccup stop the app booting
        logger.warning("channel rail unavailable: %s", exc)
        return None


class ChannelRail:
    """One channel, its off-chain ledger and its on-chain settlement."""

    def __init__(
        self,
        store: PaymentStore,
        chain: ChannelClient,
        guard: GuardClient,
        service: LocalAccount,
        channel_id: bytes,
        payer: str,
        threshold_atomic: int,
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.chain = chain
        self.guard = guard
        self.service = service
        self.channel_id = channel_id
        self.payer = payer
        self.threshold_atomic = threshold_atomic
        self._ttl = cache_ttl_seconds
        # The public Arc RPC rate limits, so cap and balance reads are cached for
        # a few seconds and the local unsettled figure is subtracted from them.
        self._cache: dict[str, tuple[float, int]] = {}

    @property
    def channel_id_hex(self) -> str:
        return "0x" + self.channel_id.hex()

    # ---- cached chain reads ----------------------------------------------

    def _cached(self, key: str, fetch: Any) -> int:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = int(fetch())
        self._cache[key] = (now + self._ttl, value)
        return value

    def _cap_remaining_onchain(self, subject: bytes) -> int:
        return self._cached(
            f"cap:{subject.hex()}", lambda: self.guard.remaining(self.channel_id, subject)
        )

    def _outstanding_onchain(self) -> int:
        return self._cached("outstanding", lambda: self.chain.outstanding(self.channel_id))

    def invalidate(self) -> None:
        self._cache.clear()

    # ---- quoting ----------------------------------------------------------

    async def quote(self, guild_id: str, user_id: str, price_atomic: int) -> ChannelQuote:
        """The cumulative the payer should sign next, and what is left of the cap.

        The service's ledger is authoritative for the cumulative, so the payer
        never has to remember where it got to.
        """
        subject = discord_subject(guild_id, user_id)
        meter = await self.store.get_meter(self.channel_id_hex, "0x" + subject.hex())
        current = meter.cumulative_atomic if meter else 0
        unsettled = meter.unsettled_atomic if meter else 0
        cap_left = max(0, self._cap_remaining_onchain(subject) - unsettled)
        return ChannelQuote(
            channel_id=self.channel_id_hex,
            subject="0x" + subject.hex(),
            cumulative=current + price_atomic,
            valid_before=int(time.time()) + chain_config.VOUCHER_TTL_SECONDS,
            price_atomic=price_atomic,
            cap_remaining_atomic=cap_left,
        )

    # ---- recording --------------------------------------------------------

    async def record(
        self,
        guild_id: str,
        user_id: str,
        price_atomic: int,
        voucher: Voucher,
        signature: bytes,
    ) -> MeterOutcome:
        """Accept a voucher for one call, or say exactly why it was refused."""
        subject = discord_subject(guild_id, user_id)
        subject_hex = "0x" + subject.hex()

        if voucher.channel_id != self.channel_id:
            return MeterOutcome(False, "voucher is for another channel", subject_hex)
        if voucher.subject != subject:
            return MeterOutcome(False, "voucher subject does not match this user", subject_hex)
        if voucher.valid_before <= int(time.time()):
            return MeterOutcome(False, "voucher has expired", subject_hex)

        signer = self.chain.recover_voucher(voucher, signature)
        if signer.lower() != self.payer.lower():
            return MeterOutcome(
                False, f"voucher signed by {signer}, not the channel payer", subject_hex
            )

        meter = await self.store.get_meter(self.channel_id_hex, subject_hex)
        current = meter.cumulative_atomic if meter else 0
        unsettled = meter.unsettled_atomic if meter else 0
        expected = current + price_atomic
        if voucher.cumulative != expected:
            return MeterOutcome(
                False, f"cumulative should be {expected}, got {voucher.cumulative}", subject_hex
            )

        cap_left = max(0, self._cap_remaining_onchain(subject) - unsettled)
        if price_atomic > cap_left:
            return MeterOutcome(
                False,
                f"on-chain cap leaves {cap_left} atomic, this call needs {price_atomic}",
                subject_hex,
                cap_remaining_atomic=cap_left,
            )

        outstanding = self._outstanding_onchain()
        pending_total = await self.pending_total()
        if pending_total + price_atomic > outstanding:
            return MeterOutcome(
                False, "channel deposit cannot cover this call, top it up", subject_hex
            )

        updated = await self.store.bump_meter(
            self.channel_id_hex,
            subject_hex,
            guild_id,
            user_id,
            voucher.cumulative,
            json.dumps(voucher_to_dict(voucher, signature)),
        )
        return MeterOutcome(
            True,
            "metered off-chain, no transaction",
            subject_hex,
            cumulative_atomic=updated.cumulative_atomic,
            calls=updated.calls,
            unsettled_atomic=updated.unsettled_atomic,
            cap_remaining_atomic=max(0, cap_left - price_atomic),
        )

    # ---- settling ---------------------------------------------------------

    async def pending_total(self) -> int:
        rows = await self.store.unsettled_meters(self.channel_id_hex)
        return sum(r.unsettled_atomic for r in rows)

    async def should_settle(self) -> bool:
        return await self.pending_total() >= self.threshold_atomic

    async def settle(self, force: bool = False) -> ChannelSettlement | None:
        """Redeem every outstanding voucher in one transaction.

        Returns None when there is nothing to collect, or when the accrued total
        is still under the threshold and the caller did not force it.
        """
        rows = await self.store.unsettled_meters(self.channel_id_hex)
        rows = [r for r in rows if r.voucher_json]
        if not rows:
            return None
        total = sum(r.unsettled_atomic for r in rows)
        if not force and total < self.threshold_atomic:
            return None

        vouchers: list[Voucher] = []
        signatures: list[bytes] = []
        calls = 0
        for row in rows:
            voucher, signature = voucher_from_dict(json.loads(row.voucher_json))
            vouchers.append(voucher)
            signatures.append(signature)
            calls += row.calls

        sent = self.chain.redeem(self.service, self.channel_id, vouchers, signatures)
        if not sent.ok:
            logger.error("redeem reverted, tx %s", sent.tx_hash)
            raise RuntimeError(f"redeem failed on-chain: {sent.tx_hash}")

        await self.store.mark_meters_settled(
            self.channel_id_hex, {"0x" + v.subject.hex(): v.cumulative for v in vouchers}
        )
        settlement = ChannelSettlement(
            channel_id=self.channel_id_hex,
            tx_hash=sent.tx_hash,
            total_atomic=total,
            subject_count=len(vouchers),
            calls=calls,
            block_number=sent.block_number,
            gas_fee_atomic=sent.gas_cost_atomic,
        )
        await self.store.record_settlement(settlement)
        self.invalidate()
        logger.info(
            "settled %s atomic for %s subjects (%s calls) in %s",
            total,
            len(vouchers),
            calls,
            sent.tx_hash,
        )
        return settlement

    # ---- caps -------------------------------------------------------------

    async def set_cap(
        self, guild_id: str, user_id: str, limit_atomic: int, window_seconds: int
    ) -> SentTx:
        """Set one person's on-chain cap.

        The service holds cap ownership for this channel, so a community admin can
        change a limit through the bot and the change lands in the contract rather
        than in a config file.
        """
        subject = discord_subject(guild_id, user_id)
        sent = self.guard.set_subject_cap(
            self.service, self.channel_id, subject, limit_atomic, window_seconds
        )
        self.invalidate()
        return sent

    async def cap_of(self, guild_id: str, user_id: str) -> dict[str, Any]:
        subject = discord_subject(guild_id, user_id)
        cap = self.guard.cap_of(self.channel_id, subject)
        meter = await self.store.get_meter(self.channel_id_hex, "0x" + subject.hex())
        onchain_left = self._cap_remaining_onchain(subject)
        unsettled = meter.unsettled_atomic if meter else 0
        return {
            "subject": "0x" + subject.hex(),
            "limitAtomic": cap.limit_atomic,
            "windowSeconds": cap.window_seconds,
            "configured": cap.configured,
            "remainingAtomic": max(0, onchain_left - unsettled),
            "callsMetered": meter.calls if meter else 0,
            "cumulativeAtomic": meter.cumulative_atomic if meter else 0,
            "unsettledAtomic": unsettled,
        }

    # ---- reporting --------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """Everything the API and the landing page need in one read."""
        state = self.chain.state(self.channel_id)
        meters = await self.store.all_meters(self.channel_id_hex)
        settlements = await self.store.recent_channel_settlements(10)
        pending = sum(m.unsettled_atomic for m in meters)
        calls = sum(m.calls for m in meters)
        return {
            "channelId": self.channel_id_hex,
            "contracts": {
                "nanoChannel": chain_config.NANO_CHANNEL_ADDRESS,
                "spendGuard": chain_config.SPEND_GUARD_ADDRESS,
                "serviceRegistry": chain_config.SERVICE_REGISTRY_ADDRESS,
                "usdc": chain_config.USDC_ADDRESS,
                "explorer": chain_config.ARC_EXPLORER,
                "chainId": chain_config.ARC_CHAIN_ID,
            },
            "onchain": {
                "payer": state.payer,
                "service": state.service,
                "depositAtomic": state.deposit,
                "redeemedAtomic": state.redeemed,
                "outstandingAtomic": state.outstanding,
                "guarded": state.guarded,
                "closing": state.closing,
                "settled": state.settled,
            },
            "offchain": {
                "meteredCalls": calls,
                "pendingAtomic": pending,
                "thresholdAtomic": self.threshold_atomic,
                "subjects": [
                    {
                        "subject": m.subject,
                        "userId": m.user_id,
                        "guildId": m.guild_id,
                        "calls": m.calls,
                        "cumulativeAtomic": m.cumulative_atomic,
                        "settledAtomic": m.settled_atomic,
                        "unsettledAtomic": m.unsettled_atomic,
                        "capRemainingAtomic": self._cap_remaining_onchain(
                            bytes.fromhex(m.subject[2:])
                        ),
                    }
                    for m in meters
                ],
            },
            "settlements": [
                {
                    "txHash": s.tx_hash,
                    "url": chain_config.tx_url(s.tx_hash),
                    "totalAtomic": s.total_atomic,
                    "calls": s.calls,
                    "subjects": s.subject_count,
                    "block": s.block_number,
                    "gasFeeAtomic": s.gas_fee_atomic,
                    "settledAt": s.settled_at.isoformat(),
                }
                for s in settlements
            ],
        }
