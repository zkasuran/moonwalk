"""Known-answer tests for the two id helpers.

The expected values were computed from the exact preimages the contracts use, so
a change to either helper that would break agreement with the chain fails here.
The channel id value also matches the TypeScript SDK's ids test.
"""

from __future__ import annotations

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak
from web3 import Web3

from moonwalk_nanopay import channel_id, subject_id, subject_label

_PAYER = "0xdb6c6340342e71a63cd11ebac2185204b7777777"
_SERVICE = "0x00000000000000000000000000000000000000AA"
_SALT = bytes.fromhex("00" * 31 + "01")


def test_subject_id_known_values() -> None:
    assert "0x" + subject_id("123", "456").hex() == (
        "0xba5f2d41d5f8bd9684d4c5a9a8645658d2eb00aa9eb0f0ce4cc0e89f9defe9c1"
    )
    assert "0x" + subject_id("123456789012345678", "987654321098765432").hex() == (
        "0x2e2b521bafbec2ef50a277efbd911cf0e15c0afed2fc60bdad6ea51dc8f313c1"
    )


def test_subject_id_hashes_the_discord_preimage() -> None:
    assert subject_label("123", "456") == "discord:123:456"
    assert subject_id("123", "456") == keccak(text="discord:123:456")


def test_channel_id_known_value() -> None:
    assert "0x" + channel_id(_PAYER, _SERVICE, _SALT).hex() == (
        "0x8c682615d8567d6946c8b4f01ceb2582509cdca93e9359592319e5d39a7a3cec"
    )


def test_channel_id_equals_raw_abi_encode() -> None:
    expected = keccak(
        abi_encode(
            ["address", "address", "bytes32"],
            [
                Web3.to_checksum_address(_PAYER),
                Web3.to_checksum_address(_SERVICE),
                _SALT,
            ],
        )
    )
    assert channel_id(_PAYER, _SERVICE, _SALT) == expected


def test_channel_id_independent_of_casing() -> None:
    lower = channel_id(_PAYER.lower(), _SERVICE.lower(), _SALT)
    assert lower == channel_id(_PAYER, _SERVICE, _SALT)
