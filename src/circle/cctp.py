"""CCTP V2: the agent tops up its own Arc balance when it runs low.

The agent spends USDC on Arc and pays its gas in the same USDC. When that balance
drops under a threshold it has to refill itself, and the only first-party way to
move USDC onto Arc is Circle's Cross-Chain Transfer Protocol. Three steps, two
chains:

  1. `approve` then `depositForBurn` on the source chain's TokenMessengerV2,
     which burns the USDC and emits a message.
  2. poll Circle's Iris attestation service until it signs that message.
  3. `receiveMessage` on Arc's MessageTransmitterV2, which mints the USDC.

Everything here is built as raw calldata so a dry run can show exactly what would
be sent, byte for byte, without a key or a broadcast. `scripts/cctp_refill.py` is
the runnable front end.

Addresses, domains and the Iris paths were verified against the live chains and
the live API on 2026-07-29 (see ARC-FACTS-2026-07-29.md). CCTP V2 uses one
address per contract across every testnet, so Arc, Base Sepolia and Ethereum
Sepolia share them. Every amount is USDC atomic units, 6 decimals.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol, cast

import httpx
from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.crypto import keccak
from web3 import Web3
from web3.exceptions import Web3Exception
from web3.types import TxParams, Wei

from src.chain.client import ArcClient

# Same address on every testnet. Verified with `cast code` on Arc, Base Sepolia
# and Ethereum Sepolia: identical bytecode length on all three.
TOKEN_MESSENGER_V2 = "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"
MESSAGE_TRANSMITTER_V2 = "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275"
TOKEN_MINTER_V2 = "0xb43db544E2c27092c107639Ad201b3dEfAbcF192"

IRIS_SANDBOX_URL = "https://iris-api-sandbox.circle.com"
IRIS_MAINNET_URL = "https://iris-api.circle.com"

# Function signatures, kept as text so the selector is derived rather than pasted.
DEPOSIT_FOR_BURN = "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
RECEIVE_MESSAGE = "receiveMessage(bytes,bytes)"
APPROVE = "approve(address,uint256)"
ALLOWANCE = "allowance(address,address)"
BALANCE_OF = "balanceOf(address)"
LOCAL_DOMAIN = "localDomain()"
GET_LOCAL_TOKEN = "getLocalToken(uint32,bytes32)"
REMOTE_TOKEN_MESSENGER = "remoteTokenMessengers(uint32)"

# MessageTransmitterV2 emits this when a burn message is created. Decoding it from
# our own receipt lets us check that the message Iris attests is the one we sent.
MESSAGE_SENT_TOPIC = "0x" + keccak(text="MessageSent(bytes)").hex()


class FinalityThreshold(IntEnum):
    """CCTP V2 has exactly two levels. Anything at or below 1000 normalises to
    Fast, anything above to Standard, so these two values are the whole range."""

    FAST = 1000
    STANDARD = 2000


@dataclass(frozen=True)
class ChainConfig:
    """One side of a bridge: where to reach it and what its USDC is."""

    key: str
    name: str
    domain: int
    chain_id: int
    rpc_url: str
    usdc: str
    explorer: str
    # Arc charges gas in USDC through its native interface. Everywhere else the
    # submitter needs the chain's own gas token, which is the practical blocker on
    # a testnet where the USDC faucet hands out no ETH.
    gas_token: str

    def tx_url(self, tx_hash: str) -> str:
        h = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
        return f"{self.explorer.rstrip('/')}/tx/{h}"


# Domains read from the chains themselves (MessageTransmitterV2.localDomain), not
# from a doc page. RPC hosts are overridable because public endpoints rate limit.
CHAINS: dict[str, ChainConfig] = {
    "arc-testnet": ChainConfig(
        key="arc-testnet",
        name="Arc Testnet",
        domain=26,
        chain_id=5042002,
        rpc_url=os.getenv("ARC_RPC_URL", "https://rpc.testnet.arc.network"),
        usdc="0x3600000000000000000000000000000000000000",
        explorer="https://testnet.arcscan.app",
        gas_token="USDC",
    ),
    "eth-sepolia": ChainConfig(
        key="eth-sepolia",
        name="Ethereum Sepolia",
        domain=0,
        chain_id=11155111,
        rpc_url=os.getenv("ETH_SEPOLIA_RPC_URL", "https://ethereum-sepolia-rpc.publicnode.com"),
        usdc="0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
        explorer="https://sepolia.etherscan.io",
        gas_token="ETH",
    ),
    "base-sepolia": ChainConfig(
        key="base-sepolia",
        name="Base Sepolia",
        domain=6,
        chain_id=84532,
        rpc_url=os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org"),
        usdc="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        explorer="https://sepolia.basescan.org",
        gas_token="ETH",
    ),
}


def chain(key: str) -> ChainConfig:
    try:
        return CHAINS[key]
    except KeyError:
        known = ", ".join(sorted(CHAINS))
        raise KeyError(f"unknown chain {key!r}, known: {known}") from None


# ---- calldata ------------------------------------------------------------


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]


def _arg_types(signature: str) -> list[str]:
    inner = signature[signature.index("(") + 1 : signature.rindex(")")]
    return [t for t in inner.split(",") if t]


def encode_call(signature: str, *args: Any) -> str:
    """Selector plus ABI-encoded arguments, derived from the signature text.

    Raw calldata rather than a contract object, for two reasons: a dry run can
    print exactly what would be broadcast, and CCTP's ABIs do not have to be
    vendored into this repo to call three functions.
    """
    return selector(signature) + abi_encode(_arg_types(signature), list(args)).hex()


def address_to_bytes32(address: str) -> bytes:
    """CCTP passes addresses as bytes32, the 20 bytes left-padded with zeros."""
    return bytes(12) + bytes.fromhex(Web3.to_checksum_address(address)[2:])


@dataclass(frozen=True)
class BuiltCall:
    """One transaction, built but not sent."""

    label: str
    chain_key: str
    to: str
    data: str
    function: str
    args: dict[str, str] = field(default_factory=dict)
    value: int = 0

    @property
    def selector(self) -> str:
        return self.data[:10]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "chain": self.chain_key,
            "to": self.to,
            "function": self.function,
            "selector": self.selector,
            "args": self.args,
            "calldata": self.data,
        }


@dataclass(frozen=True)
class SentCall:
    """A built call after it was mined."""

    label: str
    chain_key: str
    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    url: str

    @property
    def ok(self) -> bool:
        return self.status == 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "chain": self.chain_key,
            "txHash": self.tx_hash,
            "block": self.block_number,
            "gasUsed": self.gas_used,
            "status": self.status,
            "url": self.url,
        }


# ---- Iris, Circle's attestation service ----------------------------------


@dataclass(frozen=True)
class FeeOption:
    """One row of GET /v2/burn/USDC/fees/{src}/{dst}.

    `minimum_fee_bps` really is basis points and it is not always a whole number:
    Base Sepolia to Arc reads 1.3. Iris will not attest a burn whose `maxFee` sits
    under the fee this implies, so the fee is computed and rounded up.
    """

    finality_threshold: int
    minimum_fee_bps: float

    def fee_for(self, amount: int) -> int:
        return math.ceil(amount * self.minimum_fee_bps / 10_000)


@dataclass(frozen=True)
class Attestation:
    """One entry from GET /v2/messages/{srcDomain}?transactionHash=...

    `message` and `attestation` are what `receiveMessage` takes. While Iris is
    still waiting the attestation field reads "PENDING", so `complete` checks both
    the status and the payload rather than trusting either alone.
    """

    status: str
    message: str
    attestation: str
    event_nonce: str = ""
    cctp_version: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.status == "complete"
            and self.attestation.startswith("0x")
            and self.message.startswith("0x")
        )


class IrisClient:
    """Circle's attestation service.

    Rate limit is 40 requests a second and a breach blocks every request for five
    minutes, so the poll interval defaults to the 5 seconds Circle's own guide
    uses. A 404 means the burn is not indexed yet, which is normal for the first
    few polls and not an error.
    """

    def __init__(
        self,
        base_url: str = IRIS_SANDBOX_URL,
        *,
        http: httpx.Client | None = None,
        poll_seconds: float = 5.0,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self._http = http or httpx.Client(timeout=timeout)

    def burn_fees(self, source_domain: int, destination_domain: int) -> list[FeeOption]:
        response = self._http.get(
            f"{self.base_url}/v2/burn/USDC/fees/{source_domain}/{destination_domain}"
        )
        response.raise_for_status()
        rows = response.json()
        return [
            FeeOption(
                finality_threshold=int(row["finalityThreshold"]),
                minimum_fee_bps=float(row["minimumFee"]),
            )
            for row in rows
        ]

    def fee_for(self, source_domain: int, destination_domain: int, threshold: int) -> FeeOption:
        """The fee row for one finality level. Fails loudly rather than guessing,
        because a maxFee below Iris's minimum means the burn is never attested."""
        options = self.burn_fees(source_domain, destination_domain)
        for option in options:
            if option.finality_threshold == threshold:
                return option
        levels = ", ".join(str(o.finality_threshold) for o in options)
        raise RuntimeError(f"Iris has no fee for threshold {threshold}, only {levels}")

    def fast_burn_allowance(self) -> float:
        response = self._http.get(f"{self.base_url}/v2/fastBurn/USDC/allowance")
        response.raise_for_status()
        return float(response.json()["allowance"])

    def message(self, source_domain: int, tx_hash: str) -> Attestation | None:
        """One poll. None means Iris has not indexed the burn yet (HTTP 404)."""
        response = self._http.get(
            f"{self.base_url}/v2/messages/{source_domain}",
            params={"transactionHash": tx_hash},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        messages = response.json().get("messages") or []
        if not messages:
            return None
        entry = messages[0]
        return Attestation(
            status=str(entry.get("status", "")),
            message=str(entry.get("message", "")),
            attestation=str(entry.get("attestation", "")),
            event_nonce=str(entry.get("eventNonce", "")),
            cctp_version=int(entry.get("cctpVersion", 0)),
        )

    def wait_for_attestation(
        self,
        source_domain: int,
        tx_hash: str,
        *,
        timeout: float = 1800.0,
        sleep: Callable[[float], None] = time.sleep,
        on_poll: Callable[[int, Attestation | None], None] | None = None,
    ) -> Attestation:
        """Poll until Iris signs the burn. `sleep` is injectable so a test does not
        wait, and `on_poll` is where a script prints progress."""
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            found = self.message(source_domain, tx_hash)
            if on_poll is not None:
                on_poll(attempt, found)
            if found is not None and found.complete:
                return found
            if time.monotonic() >= deadline:
                status = found.status if found else "not indexed"
                raise TimeoutError(
                    f"Iris did not attest {tx_hash} within {timeout:.0f}s (last status: {status})"
                )
            sleep(self.poll_seconds)


# ---- chain reads ---------------------------------------------------------


class ChainReader(Protocol):
    """The reads a refill decision needs. A protocol so the planner can be tested
    without an RPC, since every number below changes the plan."""

    def usdc_balance(self, address: str) -> int: ...

    def allowance(self, owner: str, spender: str) -> int: ...

    def local_domain(self) -> int: ...

    def local_token(self, remote_domain: int, remote_token: str) -> str: ...


class Web3ChainReader:
    """ChainReader over a real RPC.

    Reuses ArcClient because it is already a thin, rate-limit-aware web3 wrapper.
    Nothing in the reads is Arc specific, so the same class drives Ethereum
    Sepolia and Base Sepolia. Only the explorer helpers on ArcClient are Arc
    bound, and those are not used here.
    """

    def __init__(self, config: ChainConfig, client: ArcClient | None = None) -> None:
        self.config = config
        self.client = client or ArcClient(config.rpc_url, config.chain_id)

    def _call(self, to: str, signature: str, *args: object, out: str) -> object:
        tx: TxParams = {
            "to": ChecksumAddress(Web3.to_checksum_address(to)),
            "data": HexStr(encode_call(signature, *args)),
        }
        raw = bytes(self.client.w3.eth.call(tx))
        return abi_decode([out], raw)[0]

    def usdc_balance(self, address: str) -> int:
        value = self._call(
            self.config.usdc, BALANCE_OF, Web3.to_checksum_address(address), out="uint256"
        )
        return int(str(value))

    def allowance(self, owner: str, spender: str) -> int:
        value = self._call(
            self.config.usdc,
            ALLOWANCE,
            Web3.to_checksum_address(owner),
            Web3.to_checksum_address(spender),
            out="uint256",
        )
        return int(str(value))

    def local_domain(self) -> int:
        """MessageTransmitterV2's own view of which domain this chain is. Reading it
        is how a wrong RPC url is caught before anything is burned."""
        return int(str(self._call(MESSAGE_TRANSMITTER_V2, LOCAL_DOMAIN, out="uint32")))

    def local_token(self, remote_domain: int, remote_token: str) -> str:
        """What TokenMinterV2 will mint here for a token burned on `remote_domain`.
        A zero address means the route does not exist and the mint would fail."""
        value = self._call(
            TOKEN_MINTER_V2,
            GET_LOCAL_TOKEN,
            remote_domain,
            address_to_bytes32(remote_token),
            out="address",
        )
        return Web3.to_checksum_address(str(value))


# ---- planning ------------------------------------------------------------


@dataclass(frozen=True)
class RouteCheck:
    """Does this route exist, according to the chains themselves."""

    source_domain_expected: int
    source_domain_onchain: int
    destination_domain_expected: int
    destination_domain_onchain: int
    destination_local_token: str
    destination_token_expected: str

    @property
    def domains_match(self) -> bool:
        return (
            self.source_domain_expected == self.source_domain_onchain
            and self.destination_domain_expected == self.destination_domain_onchain
        )

    @property
    def mint_route_exists(self) -> bool:
        return (
            self.destination_local_token.lower() == self.destination_token_expected.lower()
            and int(self.destination_local_token, 16) != 0
        )

    @property
    def ok(self) -> bool:
        return self.domains_match and self.mint_route_exists

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceDomain": {
                "expected": self.source_domain_expected,
                "onchain": self.source_domain_onchain,
            },
            "destinationDomain": {
                "expected": self.destination_domain_expected,
                "onchain": self.destination_domain_onchain,
            },
            "destinationLocalToken": self.destination_local_token,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class RefillPlan:
    """Everything decided before anything is signed.

    A plan is worth having on its own: `scripts/cctp_refill.py --dry-run` prints
    one, including the calldata, and never touches a key.
    """

    source: ChainConfig
    destination: ChainConfig
    recipient: str
    sender: str
    threshold_atomic: int
    destination_balance: int
    source_balance: int
    deficit: int
    amount: int
    max_fee: int
    fee_bps: float
    finality: int
    allowance: int
    calls: list[BuiltCall]

    @property
    def needed(self) -> bool:
        """False means the destination balance is already above the threshold, and
        the honest answer is to do nothing."""
        return self.deficit > 0

    @property
    def funded(self) -> bool:
        return self.source_balance >= self.amount

    @property
    def needs_approval(self) -> bool:
        return self.allowance < self.amount

    @property
    def minimum_received(self) -> int:
        """What lands on the destination in the worst case, which is Circle taking
        the whole allowed fee."""
        return self.amount - self.max_fee

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.key,
            "sourceDomain": self.source.domain,
            "destination": self.destination.key,
            "destinationDomain": self.destination.domain,
            "sender": self.sender,
            "recipient": self.recipient,
            "thresholdAtomic": self.threshold_atomic,
            "destinationBalanceAtomic": self.destination_balance,
            "sourceBalanceAtomic": self.source_balance,
            "deficitAtomic": self.deficit,
            "amountAtomic": self.amount,
            "maxFeeAtomic": self.max_fee,
            "feeBps": self.fee_bps,
            "finalityThreshold": self.finality,
            "allowanceAtomic": self.allowance,
            "needed": self.needed,
            "funded": self.funded,
            "needsApproval": self.needs_approval,
            "minimumReceivedAtomic": self.minimum_received,
            "calls": [call.to_dict() for call in self.calls],
        }


def build_approve(config: ChainConfig, amount: int) -> BuiltCall:
    return BuiltCall(
        label="approve",
        chain_key=config.key,
        to=Web3.to_checksum_address(config.usdc),
        data=encode_call(APPROVE, Web3.to_checksum_address(TOKEN_MESSENGER_V2), amount),
        function=APPROVE,
        args={"spender": TOKEN_MESSENGER_V2, "amount": str(amount)},
    )


def build_deposit_for_burn(
    source: ChainConfig,
    destination: ChainConfig,
    *,
    amount: int,
    recipient: str,
    max_fee: int,
    finality: int = int(FinalityThreshold.FAST),
    destination_caller: str | None = None,
) -> BuiltCall:
    """The burn. `destination_caller` left unset means anyone may submit the mint,
    which is what lets the agent's own Arc wallet finish the transfer."""
    caller = address_to_bytes32(destination_caller) if destination_caller else bytes(32)
    return BuiltCall(
        label="depositForBurn",
        chain_key=source.key,
        to=Web3.to_checksum_address(TOKEN_MESSENGER_V2),
        data=encode_call(
            DEPOSIT_FOR_BURN,
            amount,
            destination.domain,
            address_to_bytes32(recipient),
            Web3.to_checksum_address(source.usdc),
            caller,
            max_fee,
            finality,
        ),
        function=DEPOSIT_FOR_BURN,
        args={
            "amount": str(amount),
            "destinationDomain": str(destination.domain),
            "mintRecipient": Web3.to_checksum_address(recipient),
            "burnToken": Web3.to_checksum_address(source.usdc),
            "destinationCaller": "0x" + caller.hex(),
            "maxFee": str(max_fee),
            "minFinalityThreshold": str(finality),
        },
    )


def build_receive_message(destination: ChainConfig, *, message: str, attestation: str) -> BuiltCall:
    """The mint. Both arguments come straight from Iris."""
    return BuiltCall(
        label="receiveMessage",
        chain_key=destination.key,
        to=Web3.to_checksum_address(MESSAGE_TRANSMITTER_V2),
        data=encode_call(
            RECEIVE_MESSAGE,
            bytes.fromhex(message.removeprefix("0x")),
            bytes.fromhex(attestation.removeprefix("0x")),
        ),
        function=RECEIVE_MESSAGE,
        args={
            "messageBytes": str(len(message.removeprefix("0x")) // 2),
            "attestationBytes": str(len(attestation.removeprefix("0x")) // 2),
        },
    )


def send_call(
    client: ArcClient,
    account: LocalAccount,
    call: BuiltCall,
    *,
    gas_buffer: float = 1.25,
    config: ChainConfig,
) -> SentCall:
    """Sign and broadcast one built call, then wait for the receipt.

    Written here rather than reusing ArcClient.send because that one takes a web3
    contract function and this module works in raw calldata. Fees follow the
    EIP-1559 shape both Arc and the Sepolia chains use, with a legacy fallback for
    a node that reports no base fee.
    """
    w3 = client.w3
    tx: TxParams = {
        "from": ChecksumAddress(Web3.to_checksum_address(account.address)),
        "to": ChecksumAddress(Web3.to_checksum_address(call.to)),
        "data": HexStr(call.data),
        "value": Wei(call.value),
        "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(account.address)),
        "chainId": config.chain_id,
    }
    base_fee = int(w3.eth.get_block("latest").get("baseFeePerGas") or 0)
    if base_fee:
        priority = int(w3.eth.max_priority_fee)
        tx["maxPriorityFeePerGas"] = Wei(priority)
        # Room for two base-fee doublings. Arc holds a 20 Gwei floor, and a
        # maxFeePerGas under the floor is admitted and then never mined, so
        # underpricing here looks like a hang rather than an error.
        tx["maxFeePerGas"] = Wei(base_fee * 2 + priority)
    else:
        tx["gasPrice"] = w3.eth.gas_price
    tx["gas"] = int(int(w3.eth.estimate_gas(tx)) * gas_buffer)
    signed = account.sign_transaction(cast("dict[str, Any]", tx))

    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    digest = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
    if not digest.startswith("0x"):
        digest = "0x" + digest
    return SentCall(
        label=call.label,
        chain_key=call.chain_key,
        tx_hash=digest,
        block_number=int(receipt["blockNumber"]),
        gas_used=int(receipt["gasUsed"]),
        status=int(receipt["status"]),
        url=config.tx_url(digest),
    )


def message_sent_from_receipt(client: ArcClient, tx_hash: str) -> str | None:
    """The `message` MessageTransmitterV2 logged for our burn.

    Compared against what Iris returns, this is the check that the attestation
    belongs to this transaction and not to some other burn in the same block.
    """
    receipt = client.w3.eth.get_transaction_receipt(HexStr(tx_hash))
    transmitter = Web3.to_checksum_address(MESSAGE_TRANSMITTER_V2)
    for log in receipt["logs"]:
        if Web3.to_checksum_address(log["address"]) != transmitter:
            continue
        topics = log["topics"]
        if not topics or "0x" + bytes(topics[0]).hex() != MESSAGE_SENT_TOPIC:
            continue
        decoded = abi_decode(["bytes"], bytes(log["data"]))[0]
        return "0x" + bytes(decoded).hex()
    return None


# CCTP V2 does not hand back the exact bytes it logged. Four fields are zero in the
# event and filled in during attestation:
#   12..44   nonce, which V2 assigns at attestation rather than at burn
#   144..148 finalityThresholdExecuted
#   312..344 feeExecuted, inside the burn body
#   344..376 expirationBlock, inside the burn body
# Measured byte for byte on both live transfers on 2026-07-29. Arc to Ethereum
# Sepolia came back with fee 0 and executed 2000, because Arc finalises in one
# block. Ethereum Sepolia to Arc came back with fee 100 units and executed 1000.
# Nothing outside those ranges moved on either.
MESSAGE_HEADER_BYTES = 148
FEE_EXECUTED_OFFSET = MESSAGE_HEADER_BYTES + 164
MESSAGE_MUTABLE_RANGES = (
    (12, 44),
    (144, 148),
    (FEE_EXECUTED_OFFSET, FEE_EXECUTED_OFFSET + 32),
    (MESSAGE_HEADER_BYTES + 196, MESSAGE_HEADER_BYTES + 228),
)


def attested_message_matches(logged: str, attested: str) -> bool:
    """Is the message Iris signed the one our burn emitted?

    Straight equality would fail for a reason that has nothing to do with
    correctness, so this allows exactly the fields Circle fills in and nothing
    else. A different amount, recipient or destination shows up as a mismatch.
    """
    ours = bytes.fromhex(logged.removeprefix("0x"))
    theirs = bytes.fromhex(attested.removeprefix("0x"))
    if len(ours) != len(theirs):
        return False
    return all(
        left == right or any(start <= index < end for start, end in MESSAGE_MUTABLE_RANGES)
        for index, (left, right) in enumerate(zip(ours, theirs))
    )


def fee_executed(attested_message: str) -> int:
    """What Circle actually charged, read out of the attested message rather than
    inferred from a balance difference."""
    raw = bytes.fromhex(attested_message.removeprefix("0x"))
    return int.from_bytes(raw[FEE_EXECUTED_OFFSET : FEE_EXECUTED_OFFSET + 32], "big")


@dataclass
class BridgeRun:
    """The record of one real bridge, in the shape the evidence file wants."""

    plan: RefillPlan
    calls: list[SentCall] = field(default_factory=list)
    attestation_status: str = ""
    event_nonce: str = ""
    attested_message_consistent: bool | None = None
    fee_charged: int = 0
    destination_balance_before: int = 0
    destination_balance_after: int = 0

    @property
    def minted(self) -> int:
        """What the destination minted: the burn amount less Circle's fee. The
        balance delta can differ from this, because on Arc the submitter pays gas
        in the same USDC and is often the recipient."""
        return self.plan.amount - self.fee_charged

    @property
    def received(self) -> int:
        return self.destination_balance_after - self.destination_balance_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "transactions": [call.to_dict() for call in self.calls],
            "attestationStatus": self.attestation_status,
            "eventNonce": self.event_nonce,
            "attestedMessageConsistent": self.attested_message_consistent,
            "feeChargedAtomic": self.fee_charged,
            "mintedAtomic": self.minted,
            "destinationBalanceBeforeAtomic": self.destination_balance_before,
            "destinationBalanceAfterAtomic": self.destination_balance_after,
            "receivedAtomic": self.received,
        }


class CctpBridge:
    """One direction of a CCTP V2 route, planned and optionally executed.

    Readers are injectable so the planning arithmetic can be tested offline. Left
    alone they talk to the two chains over RPC.
    """

    def __init__(
        self,
        source: ChainConfig,
        destination: ChainConfig,
        *,
        iris: IrisClient | None = None,
        source_reader: ChainReader | None = None,
        destination_reader: ChainReader | None = None,
    ) -> None:
        if source.key == destination.key:
            raise ValueError("source and destination must be different chains")
        self.source = source
        self.destination = destination
        self.iris = iris or IrisClient()
        self.source_reader = source_reader or Web3ChainReader(source)
        self.destination_reader = destination_reader or Web3ChainReader(destination)

    def verify_route(self) -> RouteCheck:
        """Ask both chains whether this route is what we think it is."""
        return RouteCheck(
            source_domain_expected=self.source.domain,
            source_domain_onchain=self.source_reader.local_domain(),
            destination_domain_expected=self.destination.domain,
            destination_domain_onchain=self.destination_reader.local_domain(),
            destination_local_token=self.destination_reader.local_token(
                self.source.domain, self.source.usdc
            ),
            destination_token_expected=Web3.to_checksum_address(self.destination.usdc),
        )

    def plan(
        self,
        *,
        sender: str,
        threshold: int,
        recipient: str | None = None,
        amount: int | None = None,
        finality: int = int(FinalityThreshold.FAST),
        max_fee: int | None = None,
        fee_buffer: float = 1.25,
    ) -> RefillPlan:
        """Decide whether to refill, how much, and what the two calls look like.

        `amount` defaults to exactly the deficit against the threshold, which is
        the autonomous behaviour: top up to the line and no further. `max_fee` is
        the live Iris minimum for this route plus a buffer, because a maxFee under
        the minimum is a burn that never gets attested and a fee that moves between
        planning and broadcasting would strand the funds.
        """
        who = Web3.to_checksum_address(sender)
        mint_to = Web3.to_checksum_address(recipient or sender)
        destination_balance = self.destination_reader.usdc_balance(mint_to)
        deficit = max(0, threshold - destination_balance)
        move = max(0, amount if amount is not None else deficit)
        source_balance = self.source_reader.usdc_balance(who)
        allowance = self.source_reader.allowance(who, TOKEN_MESSENGER_V2)
        option = self.iris.fee_for(self.source.domain, self.destination.domain, finality)
        fee = option.fee_for(move)
        if max_fee is not None:
            ceiling = max_fee
        elif move == 0:
            ceiling = 0
        else:
            # A maxFee of zero is legal where Iris charges nothing, but one atomic
            # unit costs $0.000001 and removes the question entirely.
            ceiling = max(1, math.ceil(fee * fee_buffer))
        calls: list[BuiltCall] = []
        if move > 0:
            if allowance < move:
                calls.append(build_approve(self.source, move))
            calls.append(
                build_deposit_for_burn(
                    self.source,
                    self.destination,
                    amount=move,
                    recipient=mint_to,
                    max_fee=ceiling,
                    finality=finality,
                )
            )
        return RefillPlan(
            source=self.source,
            destination=self.destination,
            recipient=mint_to,
            sender=who,
            threshold_atomic=threshold,
            destination_balance=destination_balance,
            source_balance=source_balance,
            deficit=deficit,
            amount=move,
            max_fee=ceiling,
            fee_bps=option.minimum_fee_bps,
            finality=finality,
            allowance=allowance,
            calls=calls,
        )

    # ---- broadcasting ----------------------------------------------------

    def client_for(self, chain_key: str) -> ArcClient:
        """The web3 client for one side. Reuses a reader's client when there is one
        so a run opens two connections, not four."""
        for config, reader in (
            (self.source, self.source_reader),
            (self.destination, self.destination_reader),
        ):
            if config.key != chain_key:
                continue
            if isinstance(reader, Web3ChainReader):
                return reader.client
            return ArcClient(config.rpc_url, config.chain_id)
        raise KeyError(f"{chain_key} is not part of this route")

    def config_for(self, chain_key: str) -> ChainConfig:
        if chain_key == self.source.key:
            return self.source
        if chain_key == self.destination.key:
            return self.destination
        raise KeyError(f"{chain_key} is not part of this route")

    def simulate(self, call: BuiltCall, sender: str) -> str | None:
        """eth_call the built transaction. None means it would succeed, a string is
        the revert as the node reported it. This is what makes a dry run worth
        running: the calldata is checked by the contract, not by us."""
        client = self.client_for(call.chain_key)
        tx: TxParams = {
            "from": ChecksumAddress(Web3.to_checksum_address(sender)),
            "to": ChecksumAddress(Web3.to_checksum_address(call.to)),
            "data": HexStr(call.data),
            "value": Wei(call.value),
        }
        try:
            client.w3.eth.call(tx)
        except Web3Exception as exc:
            return str(exc)
        return None

    def execute(
        self,
        plan: RefillPlan,
        *,
        source_account: LocalAccount,
        destination_account: LocalAccount,
        iris_timeout: float = 1800.0,
        on_step: Callable[[str], None] | None = None,
    ) -> BridgeRun:
        """Approve, burn, wait for Iris, mint. Real transactions on both chains."""

        def say(line: str) -> None:
            if on_step is not None:
                on_step(line)

        if not plan.calls:
            raise ValueError("this plan has nothing to bridge")

        run = BridgeRun(
            plan=plan,
            destination_balance_before=self.destination_reader.usdc_balance(plan.recipient),
        )
        source_client = self.client_for(self.source.key)
        destination_client = self.client_for(self.destination.key)
        for call in plan.calls:
            if call.label == "approve" and not plan.needs_approval:
                continue
            say(f"sending {call.label} on {self.source.name}")
            sent = send_call(source_client, source_account, call, config=self.source)
            run.calls.append(sent)
            if not sent.ok:
                raise RuntimeError(f"{call.label} reverted: {sent.tx_hash}")
        burn = next(call for call in run.calls if call.label == "depositForBurn")
        logged = message_sent_from_receipt(source_client, burn.tx_hash)
        say(f"burn {burn.tx_hash}, waiting for the Iris attestation")
        attestation = self.iris.wait_for_attestation(
            self.source.domain,
            burn.tx_hash,
            timeout=iris_timeout,
            on_poll=lambda attempt, found: say(
                f"  poll {attempt}: {found.status if found else 'not indexed yet'}"
            ),
        )
        run.attestation_status = attestation.status
        run.event_nonce = attestation.event_nonce
        run.fee_charged = fee_executed(attestation.message)
        if logged is not None:
            run.attested_message_consistent = attested_message_matches(logged, attestation.message)
        mint = build_receive_message(
            self.destination, message=attestation.message, attestation=attestation.attestation
        )
        say(f"sending receiveMessage on {self.destination.name}")
        sent_mint = send_call(
            destination_client, destination_account, mint, config=self.destination
        )
        run.calls.append(sent_mint)
        if not sent_mint.ok:
            raise RuntimeError(f"receiveMessage reverted: {sent_mint.tx_hash}")
        run.destination_balance_after = self.destination_reader.usdc_balance(plan.recipient)
        return run
