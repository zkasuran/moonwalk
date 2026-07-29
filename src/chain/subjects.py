"""Subjects: how an off-chain person becomes an on-chain accounting identity.

A subject is the thing a spend is booked against. The agent holds one wallet, but
each person in a Discord channel gets their own cap and their own ledger line, so
the contract needs a stable identifier for a person that is not an address.

We hash the platform, the community and the user id together. Hashing keeps
Discord ids off-chain while still being reproducible: anyone with the ids can
recompute the subject and audit that person's spend in the contract. It is not a
privacy claim, a Discord id is guessable, it is a way to keep raw ids out of chain
state while keeping the accounting checkable.
"""

from __future__ import annotations

from eth_utils.crypto import keccak

SUBJECT_PREFIX = "discord"


def discord_subject(guild_id: str, user_id: str) -> bytes:
    """The bytes32 subject for one Discord user in one server."""
    return keccak(text=f"{SUBJECT_PREFIX}:{guild_id}:{user_id}")


def subject_hex(guild_id: str, user_id: str) -> str:
    return "0x" + discord_subject(guild_id, user_id).hex()


def label_for(guild_id: str, user_id: str) -> str:
    """Human-readable preimage, for logs and receipts. Never sent on-chain."""
    return f"{SUBJECT_PREFIX}:{guild_id}:{user_id}"
