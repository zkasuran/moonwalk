"""NanopayClient: everything MoonWalk does with the NanoChannel and SpendGuard.

The split that matters is who signs and who pays gas. The payer signs everything
(the deposit authorization, every voucher, the close agreement) and never sends a
transaction. Whoever wants the money on chain submits. On Arc they pay that
gas in the same USDC they are collecting.

Reads and calldata builders need no submitter. Submitting needs a submitter
account, which any relayer or the service can hold. Every amount is USDC atomic
units, the 6 decimal ERC-20 view.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.contract.contract import Contract, ContractFunction
from web3.types import TxParams

from . import ids, signing
from .addresses import (
    ARC_CHAIN_ID,
    ARC_RPC_URL,
    DEFAULT_DEPOSIT_TTL_SECONDS,
    DEFAULT_VOUCHER_TTL_SECONDS,
    MAINNET_ADDRESSES,
    ZERO_ADDRESS,
    Addresses,
)
from .types import (
    Authorization,
    Cap,
    ChannelState,
    DomainConfig,
    OpenChannelResult,
    PreparedTransaction,
    SentTx,
    SignedVoucher,
    Usage,
    Voucher,
)

_ABI_DIR = Path(__file__).parent / "abis"

_NO_SUBMITTER = (
    "NanopayClient has no submitter. Use the returned to and data with your own "
    "relayer, or construct the client with a submitter account."
)


def load_abi(name: str) -> list[dict[str, Any]]:
    """Load a committed ABI. Committed on purpose: the runtime must not depend on
    a forge build being present."""
    with (_ABI_DIR / f"{name}.json").open() as fh:
        abi: list[dict[str, Any]] = json.load(fh)
    return abi


class NanopayClient:
    """A client for the MoonWalk NanoChannel and SpendGuard on Arc mainnet."""

    def __init__(
        self,
        rpc_url: str | None = None,
        addresses: Addresses = MAINNET_ADDRESSES,
        chain_id: int = ARC_CHAIN_ID,
        submitter: LocalAccount | None = None,
    ) -> None:
        self.rpc_url = rpc_url or ARC_RPC_URL
        self.addresses = addresses
        self.chain_id = chain_id
        self._submitter = submitter
        # The public Arc RPC rate limits and web3's validation middleware asks for
        # the chain id on every request. Cache the requests that never change and
        # drop that middleware, which keeps a chatty run under the limit.
        self.w3 = Web3(
            Web3.HTTPProvider(
                self.rpc_url,
                request_kwargs={"timeout": 30},
                cache_allowed_requests=True,
            )
        )
        if "validation" in self.w3.middleware_onion:
            self.w3.middleware_onion.remove("validation")
        self._channel: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(addresses.nano_channel),
            abi=load_abi("NanoChannel"),
        )
        self._guard: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(addresses.spend_guard),
            abi=load_abi("SpendGuard"),
        )
        self._usdc: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(addresses.usdc),
            abi=load_abi("USDC"),
        )
        self._registry: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(addresses.service_registry),
            abi=load_abi("ServiceRegistry"),
        )

    @staticmethod
    def account(private_key: str) -> LocalAccount:
        """Build a local signer from a private key."""
        key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        acct: LocalAccount = Account.from_key(key)
        return acct

    @property
    def domain_config(self) -> DomainConfig:
        """The EIP-712 domain inputs the signing and digest helpers need."""
        return DomainConfig(
            chain_id=self.chain_id,
            nano_channel=self.addresses.nano_channel,
            usdc=self.addresses.usdc,
        )

    # ---- id and digest helpers, all pure ---------------------------------

    def channel_id(self, payer: str, service: str, salt: bytes) -> bytes:
        return ids.channel_id(payer, service, salt)

    def subject_id(self, guild_id: str, user_id: str) -> bytes:
        return ids.subject_id(guild_id, user_id)

    def subject_label(self, guild_id: str, user_id: str) -> str:
        return ids.subject_label(guild_id, user_id)

    def voucher_digest(self, voucher: Voucher) -> bytes:
        return signing.voucher_digest(self.domain_config, voucher)

    def close_digest(self, channel_id: bytes, redeemed: int) -> bytes:
        return signing.close_digest(self.domain_config, channel_id, redeemed)

    def voucher(
        self,
        channel_id: bytes,
        subject: bytes,
        cumulative: int,
        valid_before: int | None = None,
        ttl_seconds: int = DEFAULT_VOUCHER_TTL_SECONDS,
    ) -> Voucher:
        """Construct a voucher with a validBefore, ready to sign."""
        resolved = valid_before if valid_before is not None else int(time.time()) + ttl_seconds
        return Voucher(
            channel_id=channel_id,
            subject=subject,
            cumulative=cumulative,
            valid_before=resolved,
        )

    # ---- signing, all by the payer and gasless ---------------------------

    def sign_voucher(self, payer: LocalAccount, voucher: Voucher) -> bytes:
        return signing.sign_voucher(payer, self.domain_config, voucher)

    def sign_close(self, signer: LocalAccount, channel_id: bytes, redeemed: int) -> bytes:
        return signing.sign_close(signer, self.domain_config, channel_id, redeemed)

    def recover_voucher(self, voucher: Voucher, signature: bytes) -> str:
        return signing.recover_voucher(self.domain_config, voucher, signature)

    # ---- reads: NanoChannel ----------------------------------------------

    def channel_of(self, channel_id: bytes) -> ChannelState:
        raw = self._channel.functions.channelOf(channel_id).call()
        return ChannelState(
            payer=str(raw[0]),
            service=str(raw[1]),
            deposit=int(raw[2]),
            redeemed=int(raw[3]),
            close_at=int(raw[4]),
            guarded=bool(raw[5]),
            settled=bool(raw[6]),
        )

    def outstanding(self, channel_id: bytes) -> int:
        return int(self._channel.functions.outstanding(channel_id).call())

    def subject_redeemed(self, channel_id: bytes, subject: bytes) -> int:
        return int(self._channel.functions.subjectRedeemed(channel_id, subject).call())

    def challenge_window(self) -> int:
        return int(self._channel.functions.challengeWindow().call())

    def exists(self, channel_id: bytes) -> bool:
        return self.channel_of(channel_id).exists

    # ---- reads: SpendGuard, where the scope is the channel id ------------

    def cap_of(self, channel_id: bytes, subject: bytes) -> Cap:
        limit, window, configured = self._guard.functions.capOf(
            Web3.to_checksum_address(self.addresses.nano_channel), channel_id, subject
        ).call()
        return Cap(limit=int(limit), window=int(window), configured=bool(configured))

    def remaining(self, channel_id: bytes, subject: bytes) -> int:
        return int(
            self._guard.functions.remaining(
                Web3.to_checksum_address(self.addresses.nano_channel), channel_id, subject
            ).call()
        )

    def usage_of(self, channel_id: bytes, subject: bytes) -> Usage:
        used, window_start = self._guard.functions.usageOf(
            Web3.to_checksum_address(self.addresses.nano_channel), channel_id, subject
        ).call()
        return Usage(used=int(used), window_start=int(window_start))

    # ---- reads: USDC ------------------------------------------------------

    def usdc_balance(self, account: str) -> int:
        """USDC in atomic units (6 decimals), the ERC-20 view."""
        return int(self._usdc.functions.balanceOf(Web3.to_checksum_address(account)).call())

    def assert_arc(self) -> None:
        """Fail fast if the RPC is not the chain the addresses were deployed on."""
        actual = int(self.w3.eth.chain_id)
        if actual != self.chain_id:
            raise RuntimeError(f"connected to chain {actual}, expected {self.chain_id}")

    # ---- reads: on-chain digests, to cross-check the local ones ----------

    def voucher_hash_onchain(self, voucher: Voucher) -> bytes:
        return bytes(self._channel.functions.voucherHash(voucher.as_tuple()).call())

    def close_hash_onchain(self, channel_id: bytes, redeemed: int) -> bytes:
        return bytes(self._channel.functions.closeHash(channel_id, redeemed).call())

    def open_hash_onchain(
        self,
        service: str,
        salt: bytes,
        guarded: bool,
        cap_owner: str,
        deposit: int,
        cap_limit: int,
        cap_window: int,
        auth_nonce: bytes,
    ) -> bytes:
        return bytes(
            self._channel.functions.openHash(
                Web3.to_checksum_address(service),
                salt,
                guarded,
                Web3.to_checksum_address(cap_owner),
                deposit,
                cap_limit,
                cap_window,
                auth_nonce,
            ).call()
        )

    def domain_separator_onchain(self) -> bytes:
        return bytes(self._channel.functions.domainSeparator().call())

    # ---- writes: build calldata, submit only if a submitter is set -------

    def open_channel(
        self,
        payer: LocalAccount,
        service: str,
        salt: bytes,
        guarded: bool,
        deposit: int,
        cap_limit: int = 0,
        cap_window: int = 0,
        cap_owner: str | None = None,
        valid_after: int = 0,
        valid_before: int | None = None,
        nonce: bytes | None = None,
        deposit_ttl_seconds: int = DEFAULT_DEPOSIT_TTL_SECONDS,
    ) -> OpenChannelResult:
        """Open and fund a channel.

        Signs the two things the payer must sign: the EIP-3009
        ReceiveWithAuthorization that moves the deposit, plus the Open struct that
        binds every channel parameter and the opening cap. Returns both
        signatures, the derived channel id and a ready transaction. Submitting it
        is anyone's job, so hand ``to`` and ``data`` to a relayer or call submit().
        """
        cfg = self.domain_config
        resolved_cap_owner = (
            Web3.to_checksum_address(cap_owner)
            if cap_owner
            else Web3.to_checksum_address(ZERO_ADDRESS)
        )
        auth_nonce = nonce if nonce is not None else signing.random_nonce()
        resolved_valid_before = (
            valid_before if valid_before is not None else int(time.time()) + deposit_ttl_seconds
        )
        authorization = signing.build_authorization(
            payer, cfg, deposit, valid_after, resolved_valid_before, auth_nonce
        )
        open_signature = signing.sign_open(
            payer,
            cfg,
            service,
            salt,
            guarded,
            resolved_cap_owner,
            deposit,
            cap_limit,
            cap_window,
            auth_nonce,
        )
        args: list[Any] = [
            Web3.to_checksum_address(service),
            salt,
            guarded,
            resolved_cap_owner,
            cap_limit,
            cap_window,
            authorization.as_tuple(),
            open_signature,
        ]
        fn = self._build_fn("open", args)
        cid = ids.channel_id(payer.address, service, salt)
        return OpenChannelResult(
            to=str(self._channel.address),
            data=str(self._channel.encode_abi("open", args=args)),
            submit=lambda: self._send(fn),
            channel_id=cid,
            authorization=authorization,
            open_signature=open_signature,
        )

    def top_up(self, channel_id: bytes, authorization: Authorization) -> PreparedTransaction:
        """Add funds to an open channel with another signed authorization."""
        args: list[Any] = [channel_id, authorization.as_tuple()]
        return self._prepared("topUp", args)

    def redeem(
        self, channel_id: bytes, signed_vouchers: list[SignedVoucher]
    ) -> PreparedTransaction:
        """Settle a batch of vouchers, paying the service in one transfer."""
        vouchers = [sv.voucher.as_tuple() for sv in signed_vouchers]
        signatures = [sv.signature for sv in signed_vouchers]
        args: list[Any] = [channel_id, vouchers, signatures]
        return self._prepared("redeem", args)

    def close_mutual(
        self, channel_id: bytes, payer_signature: bytes, service_signature: bytes
    ) -> PreparedTransaction:
        """Close immediately with both sides' signatures over the same redeemed total."""
        args: list[Any] = [channel_id, payer_signature, service_signature]
        return self._prepared("closeMutual", args)

    def request_close(self, channel_id: bytes) -> PreparedTransaction:
        """Payer asks to close. The service can still redeem until the window ends.

        The contract requires the sender to be the payer, so the submitter must be
        the payer's account.
        """
        return self._prepared("requestClose", [channel_id])

    def withdraw(self, channel_id: bytes) -> PreparedTransaction:
        """Payer takes the unspent remainder once the challenge window ends.

        The contract requires the sender to be the payer.
        """
        return self._prepared("withdraw", [channel_id])

    # ---- internal ---------------------------------------------------------

    def _build_fn(self, name: str, args: list[Any]) -> Any:
        """The bound NanoChannel ContractFunction for a name and its args."""
        return getattr(self._channel.functions, name)(*args)

    def _prepared(self, name: str, args: list[Any]) -> PreparedTransaction:
        fn = self._build_fn(name, args)
        return PreparedTransaction(
            to=str(self._channel.address),
            data=str(self._channel.encode_abi(name, args=args)),
            submit=lambda: self._send(fn),
        )

    def _send(self, call: ContractFunction) -> SentTx:
        """Sign, send and wait for one contract call. Padded gas, because an
        estimate taken before other traffic lands can come in short."""
        submitter = self._submitter
        if submitter is None:
            raise RuntimeError(_NO_SUBMITTER)
        tx: TxParams = {
            "from": submitter.address,
            "nonce": self.w3.eth.get_transaction_count(submitter.address),
            "chainId": self.chain_id,
        }
        estimated = int(call.estimate_gas({"from": submitter.address}))
        tx["gas"] = int(estimated * 1.25)
        built = call.build_transaction(tx)
        # web3's TxParams and eth-account's expected transaction dict describe the
        # same shape with different static types, so bridge them at this one point.
        signed = submitter.sign_transaction(cast("dict[str, Any]", built))
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        hash_hex = tx_hash.hex()
        if not hash_hex.startswith("0x"):
            hash_hex = "0x" + hash_hex
        return SentTx(
            tx_hash=hash_hex,
            block_number=int(receipt["blockNumber"]),
            gas_used=int(receipt["gasUsed"]),
            status=int(receipt["status"]),
            effective_gas_price=int(receipt.get("effectiveGasPrice", 0)),
        )



