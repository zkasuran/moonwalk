"""Tests for src/chain/subjects.py, the bytes32 identity a spend is booked against.

Offline by construction. A subject is keccak256 over a documented preimage, so
every test rebuilds that preimage by hand and hashes it with pycryptodome instead
of calling the helper twice and comparing it against itself.
"""

from __future__ import annotations

from Crypto.Hash import keccak

from src.chain.subjects import SUBJECT_PREFIX, discord_subject, label_for, subject_hex

# Snowflake-shaped ids, the same shape the bot sees. Nothing here is secret and
# nothing is read from .env.
GUILD = "1517400111699726488"
OTHER_GUILD = "1113004055648059402"
ALICE = "402935800371413000"
BOB = "297041135048425475"

# keccak256 of the empty string, the value Ethereum uses for the hash of empty
# code. NIST SHA3-256 pads differently and gives another digest, so this pins the
# helper below to keccak.
KECCAK_EMPTY = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def keccak256(data: bytes) -> bytes:
    """keccak-256 straight from pycryptodome, not through eth_utils, so the test
    states the preimage and the primitive itself."""
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return digest.digest()


def test_keccak_helper_is_keccak_not_sha3() -> None:
    assert keccak256(b"").hex() == KECCAK_EMPTY


def test_subject_is_keccak_of_the_documented_preimage() -> None:
    # SpendGuard.sol documents a subject as keccak256("discord:<guildId>:<userId>").
    # Anyone holding the two ids has to be able to recompute it and audit that
    # person's spend, so the preimage is part of the interface.
    assert discord_subject(GUILD, ALICE) == keccak256(f"discord:{GUILD}:{ALICE}".encode())
    assert SUBJECT_PREFIX == "discord"


def test_subject_is_bytes32() -> None:
    subject = discord_subject(GUILD, ALICE)
    assert isinstance(subject, bytes)
    assert len(subject) == 32  # the contract takes bytes32, web3 encodes nothing else


def test_subject_is_stable() -> None:
    assert discord_subject(GUILD, ALICE) == discord_subject(GUILD, ALICE)


def test_subject_is_distinct_per_user() -> None:
    assert discord_subject(GUILD, ALICE) != discord_subject(GUILD, BOB)


def test_subject_is_distinct_per_guild() -> None:
    # Same person in two servers is two ledger lines, so each server caps its own
    # spend and neither can drain the other's allowance.
    assert discord_subject(GUILD, ALICE) != discord_subject(OTHER_GUILD, ALICE)


def test_subject_hex_is_prefixed_and_lowercase() -> None:
    value = subject_hex(GUILD, ALICE)
    assert value.startswith("0x")
    assert len(value) == 66  # 0x plus 64 hex characters
    assert value == value.lower()
    assert value == "0x" + discord_subject(GUILD, ALICE).hex()


def test_label_is_the_preimage_of_the_subject() -> None:
    # The label goes in logs and receipts. Hashing it has to land back on the
    # subject, otherwise a receipt cannot be checked against chain state.
    label = label_for(GUILD, ALICE)
    assert label == f"discord:{GUILD}:{ALICE}"
    assert keccak256(label.encode()) == discord_subject(GUILD, ALICE)


def test_ids_are_hashed_verbatim() -> None:
    # No trimming and no int cast, so "007" and "7" are different subjects and a
    # padded id is a different person. Callers pass the raw snowflake string.
    assert discord_subject(GUILD, "007") != discord_subject(GUILD, "7")
    assert discord_subject(GUILD, f" {ALICE}") != discord_subject(GUILD, ALICE)
