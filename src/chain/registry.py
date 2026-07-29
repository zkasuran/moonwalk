"""ServiceRegistry client: the priced catalog, on-chain.

The agent reads what it may buy from here, so the price it paid and the approval
that made a service buyable are both public facts rather than rows in one app's
database.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3

from . import config
from .client import ArcClient, SentTx


def discord_namespace(guild_id: str) -> bytes:
    """One Discord server, one namespace. Matches subjects.discord_subject."""
    return keccak(text=f"discord:{guild_id}")


@dataclass(frozen=True)
class ServiceListing:
    service_id: bytes
    namespace: bytes
    lister: str
    pay_to: str
    asset: str
    price_atomic: int
    verified: bool
    enabled: bool
    name: str
    description: str
    endpoint: str

    @property
    def buyable(self) -> bool:
        return self.verified and self.enabled

    @property
    def price_display(self) -> str:
        return f"${self.price_atomic / 1_000_000:.4f}"


class RegistryClient:
    def __init__(self, client: ArcClient) -> None:
        self.client = client

    def service_id(self, namespace: bytes, name: str) -> bytes:
        return bytes(self.client.registry.functions.serviceIdOf(namespace, name).call())

    def namespace_admin(self, namespace: bytes) -> str:
        return str(self.client.registry.functions.namespaceAdmin(namespace).call())

    def is_buyable(self, service_id: bytes) -> bool:
        return bool(self.client.registry.functions.isBuyable(service_id).call())

    def get(self, service_id: bytes) -> ServiceListing:
        raw = self.client.registry.functions.getService(service_id).call()
        return ServiceListing(
            service_id=service_id,
            namespace=bytes(raw[0]),
            lister=str(raw[1]),
            pay_to=str(raw[2]),
            asset=str(raw[3]),
            price_atomic=int(raw[4]),
            verified=bool(raw[5]),
            enabled=bool(raw[6]),
            name=str(raw[7]),
            description=str(raw[8]),
            endpoint=str(raw[9]),
        )

    def ids_of(self, namespace: bytes) -> list[bytes]:
        return [bytes(i) for i in self.client.registry.functions.idsOf(namespace).call()]

    def catalog(self, namespace: bytes, buyable_only: bool = True) -> list[ServiceListing]:
        """What the agent is allowed to spend on in this community."""
        listings = [self.get(i) for i in self.ids_of(namespace)]
        return [s for s in listings if s.buyable] if buyable_only else listings

    # ---- writes -----------------------------------------------------------

    def claim_namespace(self, admin: LocalAccount, namespace: bytes) -> SentTx:
        return self.client.send(admin, self.client.registry.functions.claimNamespace(namespace))

    def register(
        self,
        lister: LocalAccount,
        namespace: bytes,
        name: str,
        description: str,
        endpoint: str,
        pay_to: str,
        price_atomic: int,
        asset: str | None = None,
    ) -> tuple[bytes, SentTx]:
        call = self.client.registry.functions.register(
            namespace,
            name,
            description,
            endpoint,
            Web3.to_checksum_address(pay_to),
            Web3.to_checksum_address(asset or config.USDC_ADDRESS),
            price_atomic,
        )
        return self.service_id(namespace, name), self.client.send(lister, call)

    def set_verified(self, admin: LocalAccount, service_id: bytes, verified: bool) -> SentTx:
        return self.client.send(
            admin, self.client.registry.functions.setVerified(service_id, verified)
        )

    def set_enabled(self, caller: LocalAccount, service_id: bytes, enabled: bool) -> SentTx:
        return self.client.send(
            caller, self.client.registry.functions.setEnabled(service_id, enabled)
        )

    def set_price(
        self, lister: LocalAccount, service_id: bytes, price_atomic: int, pay_to: str
    ) -> SentTx:
        call = self.client.registry.functions.setPrice(
            service_id, price_atomic, Web3.to_checksum_address(pay_to)
        )
        return self.client.send(lister, call)
