"""NanoChannel client: sign off-chain, settle in batches.

The split that matters is who signs and who pays gas. The payer signs everything
(the deposit authorization, every voucher, the close agreement) and never sends a
transaction. Whoever wants the money on-chain submits, and on Arc they pay that
gas in the same USDC they are collecting.

Every amount is USDC atomic units, 6 decimals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3

from . import config
from .client import ArcClient, SentTx

_EIP712_DOMAIN = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

_VOUCHER_TYPE = [
    {"name": "channelId", "type": "bytes32"},
    {"name": "subject", "type": "bytes32"},
    {"name": "cumulative", "type": "uint256"},
    {"name": "validBefore", "type": "uint64"},
]

_CLOSE_TYPE = [
    {"name": "channelId", "type": "bytes32"},
    {"name": "redeemed", "type": "uint256"},
]

_RECEIVE_AUTH_TYPE = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


def random_nonce() -> bytes:
    """EIP-3009 nonces are random bytes32, not a counter, so two authorizations
    signed in the same second never collide."""
    return os.urandom(32)


_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _hex(value: bytes) -> str:
    return "0x" + value.hex()


def _bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def voucher_to_dict(voucher: Voucher, signature: bytes) -> dict[str, Any]:
    """Wire and storage form of a signed voucher.

    Hex strings throughout, so the same shape survives a JSON column, an HTTP
    header and a log line without a custom codec.
    """
    return {
        "channelId": _hex(voucher.channel_id),
        "subject": _hex(voucher.subject),
        "cumulative": voucher.cumulative,
        "validBefore": voucher.valid_before,
        "signature": _hex(signature),
    }


def voucher_from_dict(data: dict[str, Any]) -> tuple[Voucher, bytes]:
    voucher = Voucher(
        channel_id=_bytes(str(data["channelId"])),
        subject=_bytes(str(data["subject"])),
        cumulative=int(data["cumulative"]),
        valid_before=int(data["validBefore"]),
    )
    return voucher, _bytes(str(data["signature"]))


_DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_VOUCHER_TYPEHASH = keccak(
    text="Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)"
)


@dataclass(frozen=True)
class Voucher:
    """One signed statement: this subject has consumed this much in total."""

    channel_id: bytes
    subject: bytes
    cumulative: int
    valid_before: int

    def as_tuple(self) -> tuple[bytes, bytes, int, int]:
        return (self.channel_id, self.subject, self.cumulative, self.valid_before)


@dataclass(frozen=True)
class Authorization:
    """A signed EIP-3009 authorization that funds a channel."""

    payer: str
    value: int
    valid_after: int
    valid_before: int
    nonce: bytes
    signature: bytes

    def as_tuple(self) -> tuple[str, int, int, int, bytes, bytes]:
        return (
            self.payer,
            self.value,
            self.valid_after,
            self.valid_before,
            self.nonce,
            self.signature,
        )


@dataclass(frozen=True)
class ChannelState:
    payer: str
    service: str
    deposit: int
    redeemed: int
    close_at: int
    guarded: bool
    settled: bool

    @property
    def outstanding(self) -> int:
        return 0 if self.settled else self.deposit - self.redeemed

    @property
    def closing(self) -> bool:
        return self.close_at != 0 and not self.settled


class ChannelClient:
    """Everything MoonWalk does with the NanoChannel contract."""

    def __init__(self, client: ArcClient) -> None:
        self.client = client
        self.address = Web3.to_checksum_address(config.NANO_CHANNEL_ADDRESS)

    # ---- ids and reads ----------------------------------------------------

    def channel_id(self, payer: str, service: str, salt: bytes) -> bytes:
        """Derived locally and on-chain the same way, so either side can compute
        it without a round trip."""
        return bytes(
            self.client.channel.functions.channelIdOf(
                Web3.to_checksum_address(payer), Web3.to_checksum_address(service), salt
            ).call()
        )

    def state(self, channel_id: bytes) -> ChannelState:
        raw = self.client.channel.functions.channelOf(channel_id).call()
        return ChannelState(
            payer=str(raw[0]),
            service=str(raw[1]),
            deposit=int(raw[2]),
            redeemed=int(raw[3]),
            close_at=int(raw[4]),
            guarded=bool(raw[5]),
            settled=bool(raw[6]),
        )

    def exists(self, channel_id: bytes) -> bool:
        return self.state(channel_id).payer != "0x0000000000000000000000000000000000000000"

    def subject_redeemed(self, channel_id: bytes, subject: bytes) -> int:
        return int(self.client.channel.functions.subjectRedeemed(channel_id, subject).call())

    def outstanding(self, channel_id: bytes) -> int:
        return int(self.client.channel.functions.outstanding(channel_id).call())

    def voucher_hash_onchain(self, voucher: Voucher) -> bytes:
        """The digest the contract will check. Used to prove the local EIP-712
        encoding matches the contract's, instead of trusting two implementations
        to agree."""
        return bytes(self.client.channel.functions.voucherHash(voucher.as_tuple()).call())

    def close_hash_onchain(self, channel_id: bytes, redeemed: int) -> bytes:
        return bytes(self.client.channel.functions.closeHash(channel_id, redeemed).call())

    # ---- the same hashing, computed locally -------------------------------

    @staticmethod
    def channel_id_local(payer: str, service: str, salt: bytes) -> bytes:
        """The channel id without touching the chain.

        Same derivation as NanoChannel.channelIdOf, so a service can address a
        channel on startup with no RPC call. channel_id() asks the contract, and
        the two are asserted equal in the tests.
        """
        return keccak(
            abi_encode(
                ["address", "address", "bytes32"],
                [Web3.to_checksum_address(payer), Web3.to_checksum_address(service), salt],
            )
        )

    def domain_separator_local(self) -> bytes:
        """EIP-712 domain separator built from scratch, no contract call."""
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    _DOMAIN_TYPEHASH,
                    keccak(text=config.CHANNEL_DOMAIN_NAME),
                    keccak(text=config.CHANNEL_DOMAIN_VERSION),
                    self.client.chain_id,
                    self.address,
                ],
            )
        )

    def voucher_digest_local(self, voucher: Voucher) -> bytes:
        """The digest we sign, derived independently of the contract.

        Compare this against voucher_hash_onchain and a mismatch shows up as a
        failed assertion rather than as a settlement that reverts in production.
        """
        struct_hash = keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "uint64"],
                [
                    _VOUCHER_TYPEHASH,
                    voucher.channel_id,
                    voucher.subject,
                    voucher.cumulative,
                    voucher.valid_before,
                ],
            )
        )
        return keccak(b"\x19\x01" + self.domain_separator_local() + struct_hash)

    # ---- signing, all off-chain and gasless -------------------------------

    def sign_deposit(
        self, payer: LocalAccount, value: int, ttl_seconds: int = 3600
    ) -> Authorization:
        """Authorize the channel contract to pull `value` USDC from the payer.

        EIP-3009 ReceiveWithAuthorization, so only the channel (the `to`) can
        redeem it. The payer signs and stops there: no approve, no gas, no
        transaction.
        """
        now = int(self.client.w3.eth.get_block("latest")["timestamp"])
        nonce = random_nonce()
        message = {
            "from": payer.address,
            "to": self.address,
            "value": value,
            "validAfter": 0,
            "validBefore": now + ttl_seconds,
            "nonce": nonce,
        }
        full: dict[str, Any] = {
            "types": {
                "EIP712Domain": _EIP712_DOMAIN,
                "ReceiveWithAuthorization": _RECEIVE_AUTH_TYPE,
            },
            "primaryType": "ReceiveWithAuthorization",
            "domain": {
                "name": config.USDC_DOMAIN_NAME,
                "version": config.USDC_DOMAIN_VERSION,
                "chainId": self.client.chain_id,
                "verifyingContract": Web3.to_checksum_address(config.USDC_ADDRESS),
            },
            "message": message,
        }
        signed = payer.sign_message(encode_typed_data(full_message=full))
        return Authorization(
            payer=payer.address,
            value=value,
            valid_after=0,
            valid_before=now + ttl_seconds,
            nonce=nonce,
            signature=bytes(signed.signature),
        )

    def voucher(self, channel_id: bytes, subject: bytes, cumulative: int) -> Voucher:
        now = int(self.client.w3.eth.get_block("latest")["timestamp"])
        return Voucher(
            channel_id=channel_id,
            subject=subject,
            cumulative=cumulative,
            valid_before=now + config.VOUCHER_TTL_SECONDS,
        )

    def _channel_domain(self) -> dict[str, Any]:
        return {
            "name": config.CHANNEL_DOMAIN_NAME,
            "version": config.CHANNEL_DOMAIN_VERSION,
            "chainId": self.client.chain_id,
            "verifyingContract": self.address,
        }

    def sign_voucher(self, payer: LocalAccount, voucher: Voucher) -> bytes:
        """Sign one cumulative total. This is what a metered call costs the payer:
        a signature, no transaction, no gas, no round trip to a chain."""
        full: dict[str, Any] = {
            "types": {"EIP712Domain": _EIP712_DOMAIN, "Voucher": _VOUCHER_TYPE},
            "primaryType": "Voucher",
            "domain": self._channel_domain(),
            "message": {
                "channelId": voucher.channel_id,
                "subject": voucher.subject,
                "cumulative": voucher.cumulative,
                "validBefore": voucher.valid_before,
            },
        }
        signed = payer.sign_message(encode_typed_data(full_message=full))
        return bytes(signed.signature)

    def sign_close(self, signer: LocalAccount, channel_id: bytes, redeemed: int) -> bytes:
        """Agree that `redeemed` is the final figure. Both sides sign the same
        digest, so neither can close on a stale number."""
        full: dict[str, Any] = {
            "types": {"EIP712Domain": _EIP712_DOMAIN, "Close": _CLOSE_TYPE},
            "primaryType": "Close",
            "domain": self._channel_domain(),
            "message": {"channelId": channel_id, "redeemed": redeemed},
        }
        signed = signer.sign_message(encode_typed_data(full_message=full))
        return bytes(signed.signature)

    def recover_voucher(self, voucher: Voucher, signature: bytes) -> str:
        """Who signed this voucher.

        The service checks this before it hands over the goods. A voucher it
        cannot redeem is worth nothing, so verifying the signature here is what
        keeps the off-chain ledger honest without a chain round trip.
        """
        full: dict[str, Any] = {
            "types": {"EIP712Domain": _EIP712_DOMAIN, "Voucher": _VOUCHER_TYPE},
            "primaryType": "Voucher",
            "domain": self._channel_domain(),
            "message": {
                "channelId": voucher.channel_id,
                "subject": voucher.subject,
                "cumulative": voucher.cumulative,
                "validBefore": voucher.valid_before,
            },
        }
        return str(
            Account.recover_message(encode_typed_data(full_message=full), signature=signature)
        )

    # ---- submitting, by anyone willing to pay the gas ---------------------

    def open(
        self,
        submitter: LocalAccount,
        service: str,
        salt: bytes,
        guarded: bool,
        auth: Authorization,
        cap_owner: str | None = None,
    ) -> tuple[bytes, SentTx]:
        """Open and fund. `cap_owner` administers the caps; leave it unset and the
        payer owns them, which only works if the payer is willing to send a
        transaction."""
        call = self.client.channel.functions.open(
            Web3.to_checksum_address(service),
            salt,
            guarded,
            Web3.to_checksum_address(cap_owner) if cap_owner else _ZERO_ADDRESS,
            auth.as_tuple(),
        )
        sent = self.client.send(submitter, call)
        return self.channel_id(auth.payer, service, salt), sent

    def top_up(self, submitter: LocalAccount, channel_id: bytes, auth: Authorization) -> SentTx:
        return self.client.send(
            submitter, self.client.channel.functions.topUp(channel_id, auth.as_tuple())
        )

    def redeem(
        self,
        submitter: LocalAccount,
        channel_id: bytes,
        vouchers: list[Voucher],
        signatures: list[bytes],
    ) -> SentTx:
        """Settle a batch. One transaction, one USDC transfer, however many calls
        those cumulative totals represent."""
        call = self.client.channel.functions.redeem(
            channel_id, [v.as_tuple() for v in vouchers], signatures
        )
        return self.client.send(submitter, call)

    def close_mutual(
        self, submitter: LocalAccount, channel_id: bytes, payer_sig: bytes, service_sig: bytes
    ) -> SentTx:
        call = self.client.channel.functions.closeMutual(channel_id, payer_sig, service_sig)
        return self.client.send(submitter, call)

    def request_close(self, payer: LocalAccount, channel_id: bytes) -> SentTx:
        """Unilateral path for when the service stops responding. The service can
        still redeem until the challenge window ends."""
        return self.client.send(payer, self.client.channel.functions.requestClose(channel_id))

    def withdraw(self, payer: LocalAccount, channel_id: bytes) -> SentTx:
        return self.client.send(payer, self.client.channel.functions.withdraw(channel_id))

    def challenge_window(self) -> int:
        return int(self.client.channel.functions.challengeWindow().call())
