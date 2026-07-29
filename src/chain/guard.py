"""SpendGuard client: per-subject caps, set once and enforced by the contract.

The operator sets a cap for a person, then nothing the backend does can spend past
it. That is the whole difference from a budget in a database.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from web3 import Web3

from . import config
from .client import ArcClient, SentTx


@dataclass(frozen=True)
class Cap:
    limit_atomic: int
    window_seconds: int
    configured: bool

    @property
    def limit_usdc(self) -> float:
        return self.limit_atomic / 1_000_000


class GuardClient:
    """Reads and writes SpendGuard for one channel scope."""

    def __init__(self, client: ArcClient, channel_address: str | None = None) -> None:
        self.client = client
        self.app = Web3.to_checksum_address(channel_address or config.NANO_CHANNEL_ADDRESS)

    # ---- reads ------------------------------------------------------------

    def scope_owner(self, scope: bytes) -> str:
        return str(self.client.guard.functions.scopeOwner(self.app, scope).call())

    def cap_of(self, scope: bytes, subject: bytes) -> Cap:
        limit, window, configured = self.client.guard.functions.capOf(
            self.app, scope, subject
        ).call()
        return Cap(int(limit), int(window), bool(configured))

    def remaining(self, scope: bytes, subject: bytes) -> int:
        return int(self.client.guard.functions.remaining(self.app, scope, subject).call())

    def used(self, scope: bytes, subject: bytes) -> int:
        used, _window_start = self.client.guard.functions.usageOf(self.app, scope, subject).call()
        return int(used)

    # ---- writes (the scope owner, which is the channel's payer) ------------

    def set_default_cap(
        self, owner: LocalAccount, scope: bytes, limit_atomic: int, window_seconds: int
    ) -> SentTx:
        call = self.client.guard.functions.setDefaultCap(
            self.app, scope, limit_atomic, window_seconds
        )
        return self.client.send(owner, call)

    def set_subject_cap(
        self,
        owner: LocalAccount,
        scope: bytes,
        subject: bytes,
        limit_atomic: int,
        window_seconds: int,
    ) -> SentTx:
        call = self.client.guard.functions.setSubjectCap(
            self.app, scope, subject, limit_atomic, window_seconds
        )
        return self.client.send(owner, call)
