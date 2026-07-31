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

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


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

    def max_price(self, namespace: bytes) -> int:
        """The namespace ceiling, 0 when the admin has not set one."""
        return int(self.client.registry.functions.namespaceMaxPrice(namespace).call())

    def set_max_price(self, admin: LocalAccount, namespace: bytes, max_price_atomic: int) -> SentTx:
        return self.client.send(
            admin, self.client.registry.functions.setMaxPrice(namespace, max_price_atomic)
        )


class NamespaceNotOwned(RuntimeError):
    """Someone else holds the namespace, so this operator cannot verify listings."""

    def __init__(self, namespace: bytes, admin: str) -> None:
        super().__init__(f"namespace 0x{namespace.hex()} belongs to {admin}")
        self.namespace = namespace
        self.admin = admin


@dataclass(frozen=True)
class PublishedListing:
    """What went on-chain for one `/sell`."""

    service_id: bytes
    listing: SentTx
    #  Set only when this listing was the first in its namespace and had to claim it.
    namespace_claim: SentTx | None = None
    max_price: SentTx | None = None

    @property
    def service_id_hex(self) -> str:
        return "0x" + self.service_id.hex()


class RegistryPublisher:
    """Writes a community's marketplace onto the ServiceRegistry.

    A Discord member who lists a service has no wallet and no gas, so the operator
    submits the transaction and the listing's `payTo` is the member's own address:
    the USDC still goes to them, while the price, the endpoint and the approval stop
    being rows in this app's database and become public facts anyone can read.

    The operator also holds the namespace, which is what makes `/verify-service` the
    decision the contract checks rather than a flag in SQLite. The price ceiling the
    API documents is pinned on-chain at the same time, so the contract refuses an
    over-priced listing even if this service is wrong about its own rules.
    """

    def __init__(
        self, registry: RegistryClient, operator: LocalAccount, max_price_atomic: int
    ) -> None:
        self.registry = registry
        self.operator = operator
        self.max_price_atomic = max_price_atomic

    @property
    def address(self) -> str:
        return str(self.operator.address)

    def ensure_namespace(self, namespace: bytes) -> tuple[SentTx | None, SentTx | None]:
        """Claim the namespace on first use and pin the ceiling. Idempotent."""
        admin = self.registry.namespace_admin(namespace)
        claim: SentTx | None = None
        if admin == ZERO_ADDRESS:
            claim = self.registry.claim_namespace(self.operator, namespace)
        elif admin.lower() != self.operator.address.lower():
            raise NamespaceNotOwned(namespace, admin)
        ceiling: SentTx | None = None
        if self.registry.max_price(namespace) != self.max_price_atomic:
            ceiling = self.registry.set_max_price(self.operator, namespace, self.max_price_atomic)
        return claim, ceiling

    def publish(
        self,
        namespace: bytes,
        name: str,
        description: str,
        endpoint: str,
        pay_to: str,
        price_atomic: int,
    ) -> PublishedListing:
        claim, ceiling = self.ensure_namespace(namespace)
        service_id, sent = self.registry.register(
            self.operator, namespace, name, description, endpoint, pay_to, price_atomic
        )
        return PublishedListing(
            service_id=service_id, listing=sent, namespace_claim=claim, max_price=ceiling
        )

    def verify(self, service_id: bytes, verified: bool = True) -> SentTx:
        return self.registry.set_verified(self.operator, service_id, verified)

    def catalog(self, namespace: bytes, buyable_only: bool = False) -> list[ServiceListing]:
        return self.registry.catalog(namespace, buyable_only)


def build_publisher(max_price_atomic: int) -> RegistryPublisher | None:
    """Wire the publisher from the environment, or return None if it is not ready.

    Needs the service key, since the service is what submits and pays the gas. With
    no key the marketplace has no way to make a listing public, and the endpoints
    say so rather than writing a private row and calling it listed.
    """
    from ..payments import config as pay_config

    key = pay_config.FACILITATOR_PRIVATE_KEY or pay_config.DEPLOYER_PRIVATE_KEY
    if not key:
        return None
    try:
        client = ArcClient()
        operator = ArcClient.account(key)
    except Exception:  # noqa: BLE001 - no RPC or a bad key means no publisher
        return None
    return RegistryPublisher(RegistryClient(client), operator, max_price_atomic)
