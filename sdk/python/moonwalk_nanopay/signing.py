"""EIP-712 typed data, digests and signers for the MoonWalk NanoChannel.

The payer signs everything (the deposit authorization, every voucher, the close
agreement) and never sends a transaction. These helpers are pure and offline:
they build the exact typed data the contracts hash, so a signature and its digest
can never drift. A caller can sign with no chain round trip.
"""

from __future__ import annotations

import os
from typing import Any

from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3

from .addresses import (
    NANO_CHANNEL_DOMAIN_NAME,
    NANO_CHANNEL_DOMAIN_VERSION,
    USDC_DOMAIN_NAME,
    USDC_DOMAIN_VERSION,
)
from .types import Authorization, DomainConfig, Voucher

# ---- EIP-712 field lists ----------------------------------------------------
# Field order mirrors the struct order in NanoChannel.sol and USDC. Order is
# load bearing: a reorder changes the typehash and every signature breaks.

_EIP712_DOMAIN: list[dict[str, str]] = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

OPEN_TYPE: list[dict[str, str]] = [
    {"name": "service", "type": "address"},
    {"name": "salt", "type": "bytes32"},
    {"name": "guarded", "type": "bool"},
    {"name": "capOwner", "type": "address"},
    {"name": "deposit", "type": "uint256"},
    {"name": "capLimit", "type": "uint256"},
    {"name": "capWindow", "type": "uint64"},
    {"name": "authNonce", "type": "bytes32"},
]

VOUCHER_TYPE: list[dict[str, str]] = [
    {"name": "channelId", "type": "bytes32"},
    {"name": "subject", "type": "bytes32"},
    {"name": "cumulative", "type": "uint256"},
    {"name": "validBefore", "type": "uint64"},
]

CLOSE_TYPE: list[dict[str, str]] = [
    {"name": "channelId", "type": "bytes32"},
    {"name": "redeemed", "type": "uint256"},
]

RECEIVE_WITH_AUTHORIZATION_TYPE: list[dict[str, str]] = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


def random_nonce() -> bytes:
    """A fresh 32 byte EIP-3009 nonce. Random, not a counter, so two never collide."""
    return os.urandom(32)


def _channel_domain(cfg: DomainConfig) -> dict[str, Any]:
    return {
        "name": NANO_CHANNEL_DOMAIN_NAME,
        "version": NANO_CHANNEL_DOMAIN_VERSION,
        "chainId": cfg.chain_id,
        "verifyingContract": Web3.to_checksum_address(cfg.nano_channel),
    }


def _usdc_domain(cfg: DomainConfig) -> dict[str, Any]:
    return {
        "name": USDC_DOMAIN_NAME,
        "version": USDC_DOMAIN_VERSION,
        "chainId": cfg.chain_id,
        "verifyingContract": Web3.to_checksum_address(cfg.usdc),
    }


def _digest(message: SignableMessage) -> bytes:
    """The 32 byte EIP-712 digest the payer signs and the contract recovers from.

    This is exactly what eth-account signs: keccak over the 0x19 prefix, the
    version byte, the domain separator (``header``) and the struct hash (``body``).
    """
    return bytes(keccak(b"\x19" + message.version + message.header + message.body))


# ---- typed data builders, one per struct the contracts check ----------------


def receive_with_authorization_typed_data(
    cfg: DomainConfig,
    from_: str,
    to: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes,
) -> dict[str, Any]:
    """Typed data for the EIP-3009 ReceiveWithAuthorization that funds a deposit."""
    return {
        "types": {
            "EIP712Domain": _EIP712_DOMAIN,
            "ReceiveWithAuthorization": RECEIVE_WITH_AUTHORIZATION_TYPE,
        },
        "primaryType": "ReceiveWithAuthorization",
        "domain": _usdc_domain(cfg),
        "message": {
            "from": Web3.to_checksum_address(from_),
            "to": Web3.to_checksum_address(to),
            "value": value,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce,
        },
    }


def open_typed_data(
    cfg: DomainConfig,
    service: str,
    salt: bytes,
    guarded: bool,
    cap_owner: str,
    deposit: int,
    cap_limit: int,
    cap_window: int,
    auth_nonce: bytes,
) -> dict[str, Any]:
    """Typed data for the Open struct the payer signs to open a channel.

    ``deposit`` is the EIP-3009 authorization value and ``auth_nonce`` its nonce,
    so the Open signature is pinned to the exact authorization that funds it.
    """
    return {
        "types": {"EIP712Domain": _EIP712_DOMAIN, "Open": OPEN_TYPE},
        "primaryType": "Open",
        "domain": _channel_domain(cfg),
        "message": {
            "service": Web3.to_checksum_address(service),
            "salt": salt,
            "guarded": guarded,
            "capOwner": Web3.to_checksum_address(cap_owner),
            "deposit": deposit,
            "capLimit": cap_limit,
            "capWindow": cap_window,
            "authNonce": auth_nonce,
        },
    }


def voucher_typed_data(cfg: DomainConfig, voucher: Voucher) -> dict[str, Any]:
    """Typed data for one voucher the payer signs per metered call."""
    return {
        "types": {"EIP712Domain": _EIP712_DOMAIN, "Voucher": VOUCHER_TYPE},
        "primaryType": "Voucher",
        "domain": _channel_domain(cfg),
        "message": {
            "channelId": voucher.channel_id,
            "subject": voucher.subject,
            "cumulative": voucher.cumulative,
            "validBefore": voucher.valid_before,
        },
    }


def close_typed_data(cfg: DomainConfig, channel_id: bytes, redeemed: int) -> dict[str, Any]:
    """Typed data for the Close agreement both sides sign for an immediate close."""
    return {
        "types": {"EIP712Domain": _EIP712_DOMAIN, "Close": CLOSE_TYPE},
        "primaryType": "Close",
        "domain": _channel_domain(cfg),
        "message": {"channelId": channel_id, "redeemed": redeemed},
    }


# ---- digests, computed locally with no RPC call -----------------------------


def voucher_digest(cfg: DomainConfig, voucher: Voucher) -> bytes:
    return _digest(encode_typed_data(full_message=voucher_typed_data(cfg, voucher)))


def open_digest(
    cfg: DomainConfig,
    service: str,
    salt: bytes,
    guarded: bool,
    cap_owner: str,
    deposit: int,
    cap_limit: int,
    cap_window: int,
    auth_nonce: bytes,
) -> bytes:
    typed = open_typed_data(
        cfg, service, salt, guarded, cap_owner, deposit, cap_limit, cap_window, auth_nonce
    )
    return _digest(encode_typed_data(full_message=typed))


def close_digest(cfg: DomainConfig, channel_id: bytes, redeemed: int) -> bytes:
    return _digest(encode_typed_data(full_message=close_typed_data(cfg, channel_id, redeemed)))


# ---- signers, all by the payer and gasless ----------------------------------


def sign_receive_with_authorization(
    payer: LocalAccount,
    cfg: DomainConfig,
    to: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes,
) -> bytes:
    """Sign the EIP-3009 ReceiveWithAuthorization that funds a deposit."""
    typed = receive_with_authorization_typed_data(
        cfg, payer.address, to, value, valid_after, valid_before, nonce
    )
    signed = payer.sign_message(encode_typed_data(full_message=typed))
    return bytes(signed.signature)


def build_authorization(
    payer: LocalAccount,
    cfg: DomainConfig,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes | None = None,
) -> Authorization:
    """Build and sign a full Authorization, the deposit half of open.

    The ``to`` is fixed to the NanoChannel, so only the channel can pull the funds.
    """
    auth_nonce = nonce if nonce is not None else random_nonce()
    signature = sign_receive_with_authorization(
        payer, cfg, cfg.nano_channel, value, valid_after, valid_before, auth_nonce
    )
    return Authorization(
        payer=payer.address,
        value=value,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=auth_nonce,
        signature=signature,
    )


def sign_open(
    payer: LocalAccount,
    cfg: DomainConfig,
    service: str,
    salt: bytes,
    guarded: bool,
    cap_owner: str,
    deposit: int,
    cap_limit: int,
    cap_window: int,
    auth_nonce: bytes,
) -> bytes:
    """Sign the Open struct, the second payer signature open() needs.

    It binds every channel parameter and the opening cap to the authorization
    that funds the deposit, so a submitter cannot flip ``guarded`` off or repoint
    ``capOwner``.
    """
    typed = open_typed_data(
        cfg, service, salt, guarded, cap_owner, deposit, cap_limit, cap_window, auth_nonce
    )
    signed = payer.sign_message(encode_typed_data(full_message=typed))
    return bytes(signed.signature)


def sign_voucher(payer: LocalAccount, cfg: DomainConfig, voucher: Voucher) -> bytes:
    """Sign one cumulative total. What a metered call costs the payer: a signature."""
    signed = payer.sign_message(encode_typed_data(full_message=voucher_typed_data(cfg, voucher)))
    return bytes(signed.signature)


def sign_close(signer: LocalAccount, cfg: DomainConfig, channel_id: bytes, redeemed: int) -> bytes:
    """Sign the Close agreement. Both sides sign the same digest."""
    typed = close_typed_data(cfg, channel_id, redeemed)
    signed = signer.sign_message(encode_typed_data(full_message=typed))
    return bytes(signed.signature)


def recover_voucher(cfg: DomainConfig, voucher: Voucher, signature: bytes) -> str:
    """Who signed this voucher.

    A service checks this before it hands over the goods, so a voucher it cannot
    redeem never gets served.
    """
    message = encode_typed_data(full_message=voucher_typed_data(cfg, voucher))
    return str(Account.recover_message(message, signature=signature))
