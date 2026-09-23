"""Typed data structures for the MoonWalk NanoChannel SDK.

Every amount is USDC atomic units, the 6 decimal ERC-20 view. Byte fields
(``channel_id``, ``subject``, ``nonce``, ``signature``) are raw ``bytes`` in
memory and hex strings on the wire.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .addresses import tx_url


def _to_hex(value: bytes) -> str:
    return "0x" + value.hex()


def _to_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


@dataclass(frozen=True)
class DomainConfig:
    """The EIP-712 domain inputs the signing and digest helpers need."""

    chain_id: int
    nano_channel: str
    usdc: str


@dataclass(frozen=True)
class Voucher:
    """One signed statement: this subject has consumed ``cumulative`` in total.

    Cumulative, not per call, so a lost voucher costs nothing and a replayed
    voucher pays nothing.
    """

    channel_id: bytes
    subject: bytes
    cumulative: int
    valid_before: int

    def as_tuple(self) -> tuple[bytes, bytes, int, int]:
        return (self.channel_id, self.subject, self.cumulative, self.valid_before)


@dataclass(frozen=True)
class Authorization:
    """A signed EIP-3009 ReceiveWithAuthorization that funds a channel.

    The ``to`` is always the NanoChannel, so only the channel can pull the funds.
    """

    payer: str
    value: int
    valid_after: int
    valid_before: int
    nonce: bytes
    signature: bytes

    def as_tuple(self) -> tuple[str, int, int, int, bytes, bytes]:
        # Field order is the contract's Authorization struct, byte for byte:
        # (from, value, validAfter, validBefore, nonce, signature).
        return (
            self.payer,
            self.value,
            self.valid_after,
            self.valid_before,
            self.nonce,
            self.signature,
        )


@dataclass(frozen=True)
class SignedVoucher:
    """A voucher plus the payer signature that makes it redeemable."""

    voucher: Voucher
    signature: bytes

    def to_wire(self) -> dict[str, Any]:
        """Hex-string form for a JSON column, an HTTP header or a log line."""
        return {
            "channelId": _to_hex(self.voucher.channel_id),
            "subject": _to_hex(self.voucher.subject),
            "cumulative": self.voucher.cumulative,
            "validBefore": self.voucher.valid_before,
            "signature": _to_hex(self.signature),
        }

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> SignedVoucher:
        voucher = Voucher(
            channel_id=_to_bytes(str(data["channelId"])),
            subject=_to_bytes(str(data["subject"])),
            cumulative=int(data["cumulative"]),
            valid_before=int(data["validBefore"]),
        )
        return cls(voucher=voucher, signature=_to_bytes(str(data["signature"])))


@dataclass(frozen=True)
class ChannelState:
    """The on-chain Channel record, as ``channel_of`` returns it."""

    payer: str
    service: str
    deposit: int
    redeemed: int
    close_at: int
    guarded: bool
    settled: bool

    @property
    def outstanding(self) -> int:
        """What is left to spend. Zero once the channel is settled."""
        return 0 if self.settled else self.deposit - self.redeemed

    @property
    def closing(self) -> bool:
        """True once the payer has requested a close and it has not settled."""
        return self.close_at != 0 and not self.settled

    @property
    def exists(self) -> bool:
        """A channel that has never been opened reads back with a zero payer."""
        return int(self.payer, 16) != 0


@dataclass(frozen=True)
class Cap:
    """A SpendGuard cap that applies to a subject right now."""

    limit: int
    window: int
    configured: bool

    @property
    def limit_usdc(self) -> float:
        return self.limit / 1_000_000


@dataclass(frozen=True)
class Usage:
    """SpendGuard usage for a subject in the current window."""

    used: int
    window_start: int


@dataclass
class PreparedTransaction:
    """A transaction built ready to submit.

    ``to`` and ``data`` are what any relayer needs. ``submit`` sends it through
    the client's submitter account when one is configured. It raises otherwise.
    """

    to: str
    data: str
    submit: Callable[[], SentTx]


@dataclass
class OpenChannelResult(PreparedTransaction):
    """What ``open_channel`` returns: both signatures, the ids and a transaction."""

    channel_id: bytes
    authorization: Authorization
    open_signature: bytes


@dataclass
class SentTx:
    """A mined transaction, reduced to what a receipt or a log line needs."""

    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    effective_gas_price: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 1

    @property
    def url(self) -> str:
        return tx_url(self.tx_hash)

    @property
    def gas_cost_atomic(self) -> int:
        """Fee in the 6 decimal USDC view.

        Gas on Arc is paid in USDC through the 18 decimal native interface, so
        dividing by 1e12 puts the fee in the same units as every MoonWalk amount.
        """
        return self.gas_used * self.effective_gas_price // 10**12
