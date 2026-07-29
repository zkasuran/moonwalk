"""Thin web3 client for Arc: contracts, signing and one place for gas.

Kept deliberately small. The payment logic lives in channel.py, guard.py and
registry.py, and this module only knows how to reach Arc, load an ABI and get a
signed transaction mined.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3
from web3.contract import Contract

from . import config

logger = logging.getLogger("moonwalk.chain")

_ABI_DIR = Path(__file__).parent / "abis"


def load_abi(name: str) -> list[dict[str, Any]]:
    """Load a committed ABI. Committed on purpose: the runtime must not depend on
    a forge build being present."""
    with (_ABI_DIR / f"{name}.json").open() as fh:
        abi: list[dict[str, Any]] = json.load(fh)
    return abi


def error_selectors(abi: list[dict[str, Any]]) -> dict[str, str]:
    """Map 4-byte selector to custom error name, so a refusal can be reported as
    "CapExceeded" instead of "execution reverted"."""
    out: dict[str, str] = {}
    for item in abi:
        if item.get("type") != "error":
            continue
        types = ",".join(str(i["type"]) for i in item.get("inputs", []))
        sig = f"{item['name']}({types})"
        out["0x" + keccak(text=sig)[:4].hex()] = str(item["name"])
    return out


def revert_name(data: str | None, *abi_names: str) -> str:
    """Name the custom error in raw revert data, if we know it."""
    if not data or not data.startswith("0x") or len(data) < 10:
        return "unknown"
    selector = data[:10].lower()
    for name in abi_names:
        found = error_selectors(load_abi(name)).get(selector)
        if found:
            return found
    return selector


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
        return config.tx_url(self.tx_hash)

    @property
    def gas_cost_atomic(self) -> int:
        """What this transaction cost in the 6 decimal USDC view.

        Gas on Arc is paid in USDC through the native interface, which is 18
        decimals, while the ERC-20 view is 6. Dividing by 1e12 puts the fee in the
        same units as every amount MoonWalk moves.
        """
        return self.gas_used * self.effective_gas_price // 10**12


class ArcClient:
    """Connection to Arc plus the contract handles MoonWalk uses."""

    def __init__(self, rpc_url: str | None = None, chain_id: int | None = None) -> None:
        self.rpc_url = rpc_url or config.ARC_RPC_URL
        self.chain_id = chain_id if chain_id is not None else config.ARC_CHAIN_ID
        # The public Arc RPC rate limits, and web3's validation middleware asks for
        # the chain id on every single request. Cache the requests that never
        # change and drop that middleware, which turns a chatty run into a quiet
        # one and keeps us under the limit.
        self.w3 = Web3(
            Web3.HTTPProvider(
                self.rpc_url,
                request_kwargs={"timeout": 30},
                cache_allowed_requests=True,
            )
        )
        if "validation" in self.w3.middleware_onion:
            self.w3.middleware_onion.remove("validation")

    # ---- handles ----------------------------------------------------------

    def contract(self, address: str, abi_name: str) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=load_abi(abi_name)
        )

    @property
    def usdc(self) -> Contract:
        return self.contract(config.USDC_ADDRESS, "USDC")

    @property
    def channel(self) -> Contract:
        return self.contract(config.NANO_CHANNEL_ADDRESS, "NanoChannel")

    @property
    def guard(self) -> Contract:
        return self.contract(config.SPEND_GUARD_ADDRESS, "SpendGuard")

    @property
    def registry(self) -> Contract:
        return self.contract(config.SERVICE_REGISTRY_ADDRESS, "ServiceRegistry")

    # ---- reads ------------------------------------------------------------

    def usdc_balance(self, address: str) -> int:
        """USDC in atomic units (6 decimals), the ERC-20 view."""
        return int(self.usdc.functions.balanceOf(Web3.to_checksum_address(address)).call())

    def tx_count(self, address: str) -> int:
        """Transactions this address has sent. The whole point of the gasless
        paths is that the payer's count stays where it started."""
        return int(self.w3.eth.get_transaction_count(Web3.to_checksum_address(address)))

    def assert_arc(self) -> None:
        """Fail fast if the RPC is not the chain the addresses were deployed on."""
        actual = int(self.w3.eth.chain_id)
        if actual != self.chain_id:
            raise RuntimeError(f"connected to chain {actual}, expected {self.chain_id}")

    # ---- writes -----------------------------------------------------------

    def send(self, account: LocalAccount, call: Any, gas_buffer: float = 1.25) -> SentTx:
        """Sign, send and wait for one contract call.

        Gas is estimated then padded, because an estimate taken before other
        traffic lands in the block can come in short.
        """
        tx: dict[str, Any] = {
            "from": account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "chainId": self.chain_id,
        }
        estimated = int(call.estimate_gas({"from": account.address}))
        tx["gas"] = int(estimated * gas_buffer)
        built = call.build_transaction(tx)
        signed = account.sign_transaction(built)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        sent = SentTx(
            tx_hash=tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash),
            block_number=int(receipt["blockNumber"]),
            gas_used=int(receipt["gasUsed"]),
            status=int(receipt["status"]),
            effective_gas_price=int(receipt.get("effectiveGasPrice", 0)),
        )
        if not sent.tx_hash.startswith("0x"):
            sent.tx_hash = "0x" + sent.tx_hash
        logger.info("tx %s status %s gas %s", sent.tx_hash, sent.status, sent.gas_used)
        return sent

    @staticmethod
    def account(private_key: str) -> LocalAccount:
        key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        acct: LocalAccount = Account.from_key(key)
        return acct
