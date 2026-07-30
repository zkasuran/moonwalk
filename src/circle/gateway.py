"""Circle Gateway, read only.

Gateway Nanopayments is the closest first-party thing to MoonWalk's own payment
channel: a buyer deposits USDC into Circle's GatewayWallet contract, signs
EIP-3009 authorizations off chain, and Circle batches them into one on-chain
settlement. It is live on Arc (domain 26, nanopayments supported).

This module reads Gateway state and nothing else. No deposit, no burn intent, no
mint. It exists so the comparison in docs/CIRCLE-INTEGRATIONS.md is written
against real numbers from Circle's own API and contracts rather than from prose,
and so an operator can check a Gateway balance from the same codebase. Building
half a second payment rail would be worse than not building one, so the write
paths are deliberately absent.

Verified live on 2026-07-29: the balances API answered for domain 26 and the
GatewayWallet contract answered the same reads on Arc testnet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx
from eth_abi.abi import decode as abi_decode
from eth_typing import ChecksumAddress, HexStr
from web3 import Web3
from web3.types import TxParams

from src.chain import config as chain_config
from src.chain.client import ArcClient

from .cctp import encode_call

GATEWAY_API_TESTNET = "https://gateway-api-testnet.circle.com"
GATEWAY_API_MAINNET = "https://gateway-api.circle.com"

# ERC-1967 proxies, verified implementations, same address on every EVM testnet.
GATEWAY_WALLET = "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"
GATEWAY_MINTER = "0x0022222ABE238Cc2C7Bb1f21003F0a260052475B"

# Arc's Gateway domain, the same number CCTP uses.
ARC_DOMAIN = 26

AVAILABLE_BALANCE = "availableBalance(address,address)"
TOTAL_BALANCE = "totalBalance(address,address)"
WITHDRAWAL_DELAY = "withdrawalDelay()"
IS_TOKEN_SUPPORTED = "isTokenSupported(address)"


@dataclass(frozen=True)
class GatewayBalance:
    """What Circle's API says a depositor has in Gateway on one domain.

    The API answers in decimal USDC ("0.001000"), not atomic units, so both views
    are exposed and the conversion happens in one place.
    """

    token: str
    domain: int
    depositor: str
    available: str
    pending_batch: str

    @staticmethod
    def _atomic(value: str) -> int:
        return int(Decimal(value) * 1_000_000)

    @property
    def available_atomic(self) -> int:
        return self._atomic(self.available)

    @property
    def pending_atomic(self) -> int:
        return self._atomic(self.pending_batch)


@dataclass(frozen=True)
class GatewayOnchainState:
    """The same balance read from the GatewayWallet contract, plus the escape
    hatch. `withdrawal_delay_seconds` is the part that matters for the comparison:
    it is how long a depositor waits to get untouched funds back without Circle."""

    depositor: str
    token: str
    available_atomic: int
    total_atomic: int
    withdrawal_delay_seconds: int
    token_supported: bool


class GatewayReader:
    """Reads Gateway state, from the API and from the contract.

    Both sources are here on purpose. The API is what an integration would use and
    the contract is what actually holds the money, so reading both is how you find
    out whether Circle's view and the chain's view agree.
    """

    def __init__(
        self,
        *,
        base_url: str = GATEWAY_API_TESTNET,
        wallet_address: str = GATEWAY_WALLET,
        token: str | None = None,
        http: httpx.Client | None = None,
        client: ArcClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.wallet_address = Web3.to_checksum_address(wallet_address)
        self.token = Web3.to_checksum_address(token or chain_config.USDC_ADDRESS)
        self._http = http or httpx.Client(timeout=timeout)
        self._client = client

    @property
    def client(self) -> ArcClient:
        if self._client is None:
            self._client = ArcClient()
        return self._client

    def api_balance(self, depositor: str, *, domain: int = ARC_DOMAIN) -> GatewayBalance:
        """POST /v1/balances. No API key: Gateway balances are public."""
        payload = {
            "token": "USDC",
            "sources": [{"domain": domain, "depositor": Web3.to_checksum_address(depositor)}],
        }
        response = self._http.post(f"{self.base_url}/v1/balances", json=payload)
        response.raise_for_status()
        body = response.json()
        balances = body.get("balances") or []
        if not balances:
            raise RuntimeError(f"Gateway returned no balance for {depositor} on domain {domain}")
        entry = balances[0]
        return GatewayBalance(
            token=str(body.get("token", "USDC")),
            domain=int(entry.get("domain", domain)),
            depositor=str(entry["depositor"]),
            available=str(entry.get("balance", "0")),
            pending_batch=str(entry.get("pendingBatch", "0")),
        )

    def _read(self, signature: str, *args: object, out: str) -> object:
        tx: TxParams = {
            "to": ChecksumAddress(self.wallet_address),
            "data": HexStr(encode_call(signature, *args)),
        }
        raw = bytes(self.client.w3.eth.call(tx))
        return abi_decode([out], raw)[0]

    def onchain_state(self, depositor: str) -> GatewayOnchainState:
        """The same numbers from the GatewayWallet contract on Arc."""
        who = Web3.to_checksum_address(depositor)
        return GatewayOnchainState(
            depositor=who,
            token=self.token,
            available_atomic=int(
                str(self._read(AVAILABLE_BALANCE, self.token, who, out="uint256"))
            ),
            total_atomic=int(str(self._read(TOTAL_BALANCE, self.token, who, out="uint256"))),
            withdrawal_delay_seconds=int(str(self._read(WITHDRAWAL_DELAY, out="uint256"))),
            token_supported=bool(self._read(IS_TOKEN_SUPPORTED, self.token, out="bool")),
        )
