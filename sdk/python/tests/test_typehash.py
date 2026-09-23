"""The point of the SDK: it agrees with the deployed NanoChannel byte for byte.

A typehash is keccak256 of the exact struct type string. If the SDK computes the
same values NanoChannel stores on chain, every signature it builds will verify
against the deployed contract. The OPEN and VOUCHER on-chain values are quoted
from the deployed contract, so these are the load-bearing checks.
"""

from __future__ import annotations

from eth_utils.crypto import keccak

from moonwalk_nanopay import (
    CLOSE_TYPE_STRING,
    ONCHAIN_OPEN_TYPEHASH,
    ONCHAIN_VOUCHER_TYPEHASH,
    OPEN_TYPE_STRING,
    RECEIVE_WITH_AUTHORIZATION_TYPE_STRING,
    VOUCHER_TYPE_STRING,
)


def _keccak_hex(text: str) -> str:
    return "0x" + keccak(text=text).hex()


def test_open_typehash_equals_onchain() -> None:
    computed = _keccak_hex(OPEN_TYPE_STRING)
    assert computed == "0x9bd5e1d916549fd486a56398e9a2230b47e1d1f8e6887cbeffc06ced82cf1959"
    assert computed == ONCHAIN_OPEN_TYPEHASH


def test_voucher_typehash_equals_onchain() -> None:
    computed = _keccak_hex(VOUCHER_TYPE_STRING)
    assert computed == "0x1938bd40683f855f94a3d72f5781fd5efefa46edf4784508f4f202211b7cc140"
    assert computed == ONCHAIN_VOUCHER_TYPEHASH


def test_close_typehash_known_value() -> None:
    # The contract stores keccak256("Close(bytes32 channelId,uint256 redeemed)").
    assert _keccak_hex(CLOSE_TYPE_STRING) == (
        "0xf654789be21e851ab207dc4158f5ecf0dd088850b97837864fff8e9ea9f09135"
    )


def test_receive_with_authorization_typehash_is_canonical() -> None:
    # The USDC EIP-3009 ReceiveWithAuthorization typehash, fixed by the standard.
    assert _keccak_hex(RECEIVE_WITH_AUTHORIZATION_TYPE_STRING) == (
        "0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8"
    )
