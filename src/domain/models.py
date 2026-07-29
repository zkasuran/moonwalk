"""Domain models for NanoPay for Discord."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class CommandStatus(str, Enum):
    QUEUED = "queued"
    AWAITING_PAYMENT = "awaiting_payment"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class CommandConfig:
    """A premium command registered by a server owner."""

    guild_id: str
    command_name: str
    # price in USDC atomic units (6 decimals) — e.g. 10000 = $0.01
    price_atomic: int
    description: str = ""
    enabled: bool = True

    @property
    def price_display(self) -> str:
        dollars = self.price_atomic / 1_000_000
        return f"${dollars:.4f}"


@dataclass
class PaymentRecord:
    """One x402 payment attempt tied to a Discord interaction."""

    payment_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    guild_id: str = ""
    channel_id: str = ""
    user_id: str = ""
    command_name: str = ""
    command_args: dict[str, str] = field(default_factory=dict)
    price_atomic: int = 0
    # Override for the x402 payTo address. Empty means the default service
    # wallet; a marketplace payment sets it to the lister's wallet so the USDC
    # settles to the member who listed the service.
    pay_to: str = ""
    status: PaymentStatus = PaymentStatus.PENDING
    tx_hash: str = ""
    payer_address: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    paid_at: datetime | None = None
    # Result from the command after payment, stored for follow-up
    result: str = ""
    # Discord interaction token (valid 15 min) for follow-up
    interaction_token: str = ""
    # Discord application id for webhook follow-up
    application_id: str = ""


@dataclass
class GuildConfig:
    """Per-guild bot configuration."""

    guild_id: str
    owner_id: str
    # Per-command configs keyed by command_name
    commands: dict[str, CommandConfig] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class ChannelMeter:
    """What one subject owes on a channel, and the newest voucher that proves it.

    The off-chain half of a nanopayment channel. `cumulative_atomic` only ever
    grows, `settled_atomic` follows it on-chain after a redeem, and the gap
    between them is money the service has earned but not collected yet.
    """

    channel_id: str = ""
    subject: str = ""
    guild_id: str = ""
    user_id: str = ""
    cumulative_atomic: int = 0
    settled_atomic: int = 0
    calls: int = 0
    # The latest signed voucher, kept verbatim so a redeem can be rebuilt from
    # the store alone if the process restarts mid-batch.
    voucher_json: str = ""
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def unsettled_atomic(self) -> int:
        return max(0, self.cumulative_atomic - self.settled_atomic)


@dataclass
class ChannelSettlement:
    """One on-chain redeem: many metered calls collapsed into one transaction."""

    settlement_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str = ""
    tx_hash: str = ""
    total_atomic: int = 0
    subject_count: int = 0
    calls: int = 0
    block_number: int = 0
    gas_fee_atomic: int = 0
    settled_at: datetime = field(default_factory=_utcnow)


@dataclass
class MarketplaceService:
    """A priced service a member listed on the per-guild marketplace.

    A member lists an HTTP endpoint, a price and the wallet that should
    receive the USDC. An admin verifies it is legit before the agent can
    discover and pay for it, so an unvetted listing never spends the agent's
    money.
    """

    service_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    guild_id: str = ""
    lister_id: str = ""  # Discord user who listed it, receives the USDC
    name: str = ""
    description: str = ""
    url: str = ""  # GET endpoint the service answers on; ?q=<arg> is appended
    price_atomic: int = 0
    wallet: str = ""  # payTo address for this service's settlements
    verified: bool = False
    verified_by: str = ""  # admin user id who approved the listing
    enabled: bool = True
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def price_display(self) -> str:
        return f"${self.price_atomic / 1_000_000:.4f}"
