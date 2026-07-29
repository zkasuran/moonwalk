"""Tests for src/chain/channel.py: EIP-712 vouchers, field order and channel state.

The hashing and signing paths have to be right byte for byte, because a digest
that disagrees with NanoChannel.sol is a settlement that reverts in production.
Every digest here is rebuilt from the EIP-712 spec rather than copied from the
implementation. The client is handed a stub in place of an ArcClient so no test
can reach an RPC.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from Crypto.Hash import keccak
from eth_account import Account
from eth_keys.datatypes import Signature
from web3 import Web3

from src.chain import config
from src.chain.channel import Authorization, ChannelClient, ChannelState, Voucher
from src.chain.client import ArcClient, load_abi

# Throwaway keys built from a constant. No .env, no wallet, no network.
PAYER = Account.from_key("0x" + "11" * 32)
SERVICE = Account.from_key("0x" + "22" * 32)

CHAIN_ID = 5042002  # Arc testnet, fixed here so the environment cannot move it
OTHER_CHAIN_ID = 1

# Channel id and amounts from the live run in evidence/channel-20260729T154745Z.json.
CHANNEL_ID = bytes.fromhex("827e325ef627b989382d46e8be57ab29850c940c63b420a43c87c8e4e5776706")
DEPOSIT = 200_000
REDEEMED = 30_000
VALID_BEFORE = 1_800_000_000  # a fixed timestamp, so nothing here reads a clock

# EIP-712 type strings. The same literals NanoChannel.sol hashes into its typehash
# constants, spelled out again so a change on either side shows up.
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
VOUCHER_TYPE = "Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)"
CLOSE_TYPE = "Close(bytes32 channelId,uint256 redeemed)"
DOMAIN_NAME = "MoonWalk NanoChannel"
DOMAIN_VERSION = "1"

# secp256k1n / 2. NanoChannel._recover rejects a signature whose s sits above this,
# so the signer has to produce the low-s half of the malleable pair.
HALF_ORDER = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0


def keccak256(data: bytes) -> bytes:
    """keccak-256 through pycryptodome rather than eth_utils, so a digest here is
    not built with the same helper the implementation uses."""
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return digest.digest()


def word(value: int) -> bytes:
    """One ABI word. abi.encode pads every static type to 32 bytes, which is why a
    uint64 like validBefore takes a full word and not eight bytes."""
    return value.to_bytes(32, "big")


def address_word(address: str) -> bytes:
    """An address in an ABI word: twelve zero bytes then the twenty address bytes."""
    return bytes(12) + bytes.fromhex(address.removeprefix("0x"))


def domain_separator(chain_id: int, contract: str) -> bytes:
    """EIP-712 domain separator from the spec: the domain typehash, the hashed name
    and version, then the chain id and the verifying contract as words."""
    return keccak256(
        keccak256(DOMAIN_TYPE.encode())
        + keccak256(DOMAIN_NAME.encode())
        + keccak256(DOMAIN_VERSION.encode())
        + word(chain_id)
        + address_word(contract)
    )


def voucher_digest(voucher: Voucher, chain_id: int, contract: str) -> bytes:
    """The digest a payer signs for one voucher, derived here from the spec."""
    struct_hash = keccak256(
        keccak256(VOUCHER_TYPE.encode())
        + voucher.channel_id
        + voucher.subject
        + word(voucher.cumulative)
        + word(voucher.valid_before)
    )
    return keccak256(b"\x19\x01" + domain_separator(chain_id, contract) + struct_hash)


def close_digest(channel_id: bytes, redeemed: int, chain_id: int, contract: str) -> bytes:
    struct_hash = keccak256(keccak256(CLOSE_TYPE.encode()) + channel_id + word(redeemed))
    return keccak256(b"\x19\x01" + domain_separator(chain_id, contract) + struct_hash)


def recover(digest: bytes, signature: bytes) -> str:
    """The address behind a 65 byte signature over `digest`.

    Recovering against the digest this test derived is the whole point: it shows
    the signature was taken over that exact hash, so signing and hashing agree.
    """
    assert len(signature) == 65
    parsed = Signature(signature_bytes=signature[:64] + bytes([signature[64] - 27]))
    return str(parsed.recover_public_key_from_msg_hash(digest).to_checksum_address())


SUBJECT = keccak256(b"discord:1517400111699726488:402935800371413000")
NONCE = keccak256(b"eip-3009 nonce")
AUTH_SIGNATURE = bytes(range(65))  # any 65 bytes, the field order test never checks it


class _OfflineClient:
    """Stands in for ArcClient.

    Hashing and signing only need the chain id, so that is all this carries.
    Anything else raises, which turns an RPC that creeps into the offline path into
    a failing test instead of a socket.
    """

    def __init__(self, chain_id: int = CHAIN_ID) -> None:
        self.chain_id = chain_id

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"offline test reached for ArcClient.{name}")


def make_client(chain_id: int = CHAIN_ID) -> ChannelClient:
    return ChannelClient(cast(ArcClient, _OfflineClient(chain_id)))


def make_state(**overrides: Any) -> ChannelState:
    """A channel state holding the live run's numbers, overridable per test."""
    fields: dict[str, Any] = {
        "payer": PAYER.address,
        "service": SERVICE.address,
        "deposit": DEPOSIT,
        "redeemed": REDEEMED,
        "close_at": 0,
        "guarded": True,
        "settled": False,
    }
    fields.update(overrides)
    return ChannelState(**fields)


def struct_fields(function_name: str, argument: str) -> list[str]:
    """Field names of one struct argument, read from the committed ABI."""
    abi = load_abi("NanoChannel")
    entry = next(i for i in abi if i.get("type") == "function" and i["name"] == function_name)
    arg = next(i for i in entry["inputs"] if i["name"] == argument)
    return [str(c["name"]) for c in arg["components"]]


@pytest.fixture
def channel() -> ChannelClient:
    return make_client()


@pytest.fixture
def voucher() -> Voucher:
    return Voucher(
        channel_id=CHANNEL_ID, subject=SUBJECT, cumulative=25_000, valid_before=VALID_BEFORE
    )


# ---- the domain -----------------------------------------------------------


def test_domain_constants_match_the_contract() -> None:
    # NanoChannel.sol hashes these two literals into its domain separator and a
    # deployed contract cannot be edited, so config has to keep matching them.
    assert config.CHANNEL_DOMAIN_NAME == DOMAIN_NAME
    assert config.CHANNEL_DOMAIN_VERSION == DOMAIN_VERSION


def test_verifying_contract_is_the_configured_channel(channel: ChannelClient) -> None:
    assert channel.address == Web3.to_checksum_address(config.NANO_CHANNEL_ADDRESS)


def test_domain_separator_matches_the_spec(channel: ChannelClient) -> None:
    assert channel.domain_separator_local() == domain_separator(CHAIN_ID, channel.address)


# ---- voucher digests ------------------------------------------------------


def test_voucher_digest_matches_the_spec(channel: ChannelClient, voucher: Voucher) -> None:
    digest = channel.voucher_digest_local(voucher)
    assert len(digest) == 32
    assert digest == voucher_digest(voucher, CHAIN_ID, channel.address)


def test_voucher_digest_is_chain_specific(voucher: Voucher) -> None:
    # chainId sits inside the domain separator, so a voucher signed for Arc is not
    # a voucher anywhere else.
    here = make_client().voucher_digest_local(voucher)
    elsewhere = make_client(OTHER_CHAIN_ID).voucher_digest_local(voucher)
    assert here != elsewhere


def test_voucher_digest_covers_every_field(channel: ChannelClient, voucher: Voucher) -> None:
    variants = [
        Voucher(bytes(32), voucher.subject, voucher.cumulative, voucher.valid_before),
        Voucher(voucher.channel_id, bytes(32), voucher.cumulative, voucher.valid_before),
        Voucher(voucher.channel_id, voucher.subject, voucher.cumulative + 1, voucher.valid_before),
        Voucher(voucher.channel_id, voucher.subject, voucher.cumulative, voucher.valid_before + 1),
    ]
    digests = {channel.voucher_digest_local(v) for v in [voucher, *variants]}
    assert len(digests) == 5  # no field is dropped from the struct hash


# ---- signing --------------------------------------------------------------


def test_sign_voucher_recovers_to_the_payer(channel: ChannelClient, voucher: Voucher) -> None:
    signature = channel.sign_voucher(PAYER, voucher)
    assert len(signature) == 65  # r, s, v. The contract rejects any other length.
    assert signature[64] in (27, 28)  # and any v outside this pair
    assert int.from_bytes(signature[32:64], "big") <= HALF_ORDER  # and the high-s twin
    digest = voucher_digest(voucher, CHAIN_ID, channel.address)
    assert recover(digest, signature) == PAYER.address
    assert channel.recover_voucher(voucher, signature) == PAYER.address


def test_a_voucher_signed_by_anyone_else_is_not_the_payer(
    channel: ChannelClient, voucher: Voucher
) -> None:
    # redeem() recovers the signer and compares it against the channel's payer, so
    # a signature from the wrong key is worth nothing.
    signature = channel.sign_voucher(SERVICE, voucher)
    assert channel.recover_voucher(voucher, signature) == SERVICE.address
    assert channel.recover_voucher(voucher, signature) != PAYER.address


def test_sign_close_recovers_to_the_signer(channel: ChannelClient) -> None:
    # closeMutual checks both signatures against the same Close digest, so both
    # sides have to land on the byte string this test builds.
    digest = close_digest(CHANNEL_ID, REDEEMED, CHAIN_ID, channel.address)
    payer_signature = channel.sign_close(PAYER, CHANNEL_ID, REDEEMED)
    service_signature = channel.sign_close(SERVICE, CHANNEL_ID, REDEEMED)
    assert len(payer_signature) == 65
    assert len(service_signature) == 65
    assert recover(digest, payer_signature) == PAYER.address
    assert recover(digest, service_signature) == SERVICE.address


def test_close_signature_is_pinned_to_the_redeemed_total(channel: ChannelClient) -> None:
    # The agreement names the figure already paid out, which is what makes a close
    # signature from before the last redeem worthless.
    for_thirty = channel.sign_close(PAYER, CHANNEL_ID, REDEEMED)
    for_thirty_one = channel.sign_close(PAYER, CHANNEL_ID, REDEEMED + 1)
    assert for_thirty != for_thirty_one


def test_the_offline_paths_never_reach_the_rpc(channel: ChannelClient, voucher: Voucher) -> None:
    channel.voucher_digest_local(voucher)
    channel.sign_voucher(PAYER, voucher)
    channel.sign_close(PAYER, CHANNEL_ID, REDEEMED)
    with pytest.raises(AssertionError):
        _ = channel.client.w3  # the stub has no node and nothing above wanted one


# ---- field order ----------------------------------------------------------


def test_voucher_as_tuple_matches_the_solidity_field_order(voucher: Voucher) -> None:
    # web3 encodes a struct argument by position and never looks at the names, so
    # the order in as_tuple is the only thing tying these values to the contract's
    # fields. Swap two and the call still encodes, it just signs other numbers.
    fields = struct_fields("voucherHash", "v")
    assert list(zip(fields, voucher.as_tuple(), strict=True)) == [
        ("channelId", CHANNEL_ID),
        ("subject", SUBJECT),
        ("cumulative", 25_000),
        ("validBefore", VALID_BEFORE),
    ]


def test_authorization_as_tuple_matches_the_solidity_field_order() -> None:
    auth = Authorization(
        payer=PAYER.address,
        value=DEPOSIT,
        valid_after=0,
        valid_before=VALID_BEFORE,
        nonce=NONCE,
        signature=AUTH_SIGNATURE,
    )
    # Same reason as the voucher: position is the contract. `from` is a Python
    # keyword, so the dataclass calls that field payer and the order carries it.
    assert list(zip(struct_fields("open", "auth"), auth.as_tuple(), strict=True)) == [
        ("from", PAYER.address),
        ("value", DEPOSIT),
        ("validAfter", 0),
        ("validBefore", VALID_BEFORE),
        ("nonce", NONCE),
        ("signature", AUTH_SIGNATURE),
    ]


def test_channel_state_reads_channel_of_in_the_declared_order() -> None:
    # ChannelClient.state() indexes the tuple channelOf returns by position, so a
    # reordered Solidity struct would quietly fill the wrong attributes.
    abi = load_abi("NanoChannel")
    entry = next(i for i in abi if i.get("type") == "function" and i["name"] == "channelOf")
    fields = [str(c["name"]) for c in entry["outputs"][0]["components"]]
    assert fields == ["payer", "service", "deposit", "redeemed", "closeAt", "guarded", "settled"]


# ---- channel state --------------------------------------------------------


def test_outstanding_is_deposit_minus_redeemed() -> None:
    # $0.20 in, $0.03 settled over 30 metered calls, $0.17 left to spend.
    assert make_state().outstanding == 170_000


def test_a_settled_channel_has_nothing_outstanding() -> None:
    # The refund has already moved, so the remainder is not spendable any more.
    # NanoChannel.outstanding() returns 0 for a settled channel and this agrees.
    assert make_state(settled=True).outstanding == 0


def test_a_fully_redeemed_channel_has_nothing_outstanding() -> None:
    assert make_state(redeemed=DEPOSIT).outstanding == 0


def test_closing_is_true_only_while_a_close_is_pending() -> None:
    assert make_state(close_at=0).closing is False
    assert make_state(close_at=VALID_BEFORE).closing is True
    # Once the channel settles the challenge window is history, not pending.
    assert make_state(close_at=VALID_BEFORE, settled=True).closing is False
