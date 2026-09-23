"""The Open struct the payer signs to open a channel, checked offline.

Field order is load bearing: a reorder changes the typehash and every open()
signature breaks. This asserts the order, rebuilds the Open digest independently,
and checks a signed Open recovers to the payer.
"""

from __future__ import annotations

from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3

from moonwalk_nanopay import (
    ARC_CHAIN_ID,
    MAINNET_ADDRESSES,
    NANO_CHANNEL_DOMAIN_NAME,
    NANO_CHANNEL_DOMAIN_VERSION,
    OPEN_TYPE_STRING,
    ZERO_ADDRESS,
    DomainConfig,
    open_digest,
    sign_open,
    signing,
)

_TEST_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

_CFG = DomainConfig(
    chain_id=ARC_CHAIN_ID,
    nano_channel=MAINNET_ADDRESSES.nano_channel,
    usdc=MAINNET_ADDRESSES.usdc,
)

_SERVICE = "0x00000000000000000000000000000000000000AA"
_SALT = bytes.fromhex("33" * 32)
_GUARDED = True
_CAP_OWNER = ZERO_ADDRESS
_DEPOSIT = 5_000_000
_CAP_LIMIT = 1_000_000
_CAP_WINDOW = 86_400
_AUTH_NONCE = bytes.fromhex("44" * 32)


def _open_args() -> tuple[str, bytes, bool, str, int, int, int, bytes]:
    return (
        _SERVICE,
        _SALT,
        _GUARDED,
        _CAP_OWNER,
        _DEPOSIT,
        _CAP_LIMIT,
        _CAP_WINDOW,
        _AUTH_NONCE,
    )


def _open_digest_by_hand() -> bytes:
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
            [
                "bytes32",
                "address",
                "bytes32",
                "bool",
                "address",
                "uint256",
                "uint256",
                "uint64",
                "bytes32",
            ],
            [
                keccak(text=OPEN_TYPE_STRING),
                Web3.to_checksum_address(_SERVICE),
                _SALT,
                _GUARDED,
                Web3.to_checksum_address(_CAP_OWNER),
                _DEPOSIT,
                _CAP_LIMIT,
                _CAP_WINDOW,
                _AUTH_NONCE,
            ],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def test_open_field_order() -> None:
    assert [field["name"] for field in signing.OPEN_TYPE] == [
        "service",
        "salt",
        "guarded",
        "capOwner",
        "deposit",
        "capLimit",
        "capWindow",
        "authNonce",
    ]


def test_open_digest_matches_independent_rebuild() -> None:
    assert open_digest(_CFG, *_open_args()) == _open_digest_by_hand()


def test_signed_open_recovers_to_payer() -> None:
    account: LocalAccount = Account.from_key(_TEST_KEY)
    signature = sign_open(account, _CFG, *_open_args())
    typed = signing.open_typed_data(_CFG, *_open_args())
    message = encode_typed_data(full_message=typed)
    assert Account.recover_message(message, signature=signature) == account.address
