"""Rebuild a voucher digest independently and check the SDK computes the same.

The SDK builds the digest through eth-account's EIP-712 encoder. This test
rebuilds it from the raw keccak256 pieces of the EIP-712 spec, the same pieces
NanoChannel hashes. Agreement means the SDK's encoding matches the contract's, so
a signed voucher recovers to the payer on chain.
"""

from __future__ import annotations

from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3

from moonwalk_nanopay import (
    ARC_CHAIN_ID,
    MAINNET_ADDRESSES,
    NANO_CHANNEL_DOMAIN_NAME,
    NANO_CHANNEL_DOMAIN_VERSION,
    VOUCHER_TYPE_STRING,
    DomainConfig,
    Voucher,
    recover_voucher,
    sign_voucher,
    voucher_digest,
)

# Hardhat account #1. A well-known throwaway test key, never a real key.
_TEST_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

_CFG = DomainConfig(
    chain_id=ARC_CHAIN_ID,
    nano_channel=MAINNET_ADDRESSES.nano_channel,
    usdc=MAINNET_ADDRESSES.usdc,
)

_VOUCHER = Voucher(
    channel_id=bytes.fromhex("11" * 32),
    subject=bytes.fromhex("22" * 32),
    cumulative=123_456,
    valid_before=1_800_000_000,
)


def _digest_by_hand() -> bytes:
    domain_typehash = keccak(
        text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    domain_separator = keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                domain_typehash,
                keccak(text=NANO_CHANNEL_DOMAIN_NAME),
                keccak(text=NANO_CHANNEL_DOMAIN_VERSION),
                ARC_CHAIN_ID,
                Web3.to_checksum_address(MAINNET_ADDRESSES.nano_channel),
            ],
        )
    )
    struct_hash = keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "uint64"],
            [
                keccak(text=VOUCHER_TYPE_STRING),
                _VOUCHER.channel_id,
                _VOUCHER.subject,
                _VOUCHER.cumulative,
                _VOUCHER.valid_before,
            ],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def test_voucher_digest_matches_independent_rebuild() -> None:
    assert voucher_digest(_CFG, _VOUCHER) == _digest_by_hand()


def test_signed_voucher_recovers_to_signer() -> None:
    account: LocalAccount = Account.from_key(_TEST_KEY)
    signature = sign_voucher(account, _CFG, _VOUCHER)
    assert recover_voucher(_CFG, _VOUCHER, signature) == account.address
