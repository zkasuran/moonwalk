"""Id helpers: how an off-chain person and a channel get stable on-chain ids.

Both are pure keccak256 derivations with no RPC call, computed the same way the
contracts compute them, so either side can address a channel or book a spend
before anything exists on chain.
"""

from __future__ import annotations

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak
from web3 import Web3

#: The platform prefix hashed into every subject.
SUBJECT_PREFIX = "discord"


def subject_id(guild_id: str, user_id: str) -> bytes:
    """The bytes32 subject for one Discord user in one server.

    A subject is what a spend is booked against. The agent holds one wallet, so
    each person needs a stable identifier that is not an address. This hashes the
    platform, the server and the user id together, the way SpendGuard expects.
    Anyone with the ids can recompute it and audit that person's spend.
    """
    return bytes(keccak(text=f"{SUBJECT_PREFIX}:{guild_id}:{user_id}"))


def subject_label(guild_id: str, user_id: str) -> str:
    """The human-readable preimage of a subject. For logs and receipts, never on chain."""
    return f"{SUBJECT_PREFIX}:{guild_id}:{user_id}"


def channel_id(payer: str, service: str, salt: bytes) -> bytes:
    """The deterministic channel id: ``keccak256(abi.encode(payer, service, salt))``.

    Derived the same way on chain (``NanoChannel.channelIdOf``) and here.
    Addresses are checksummed before encoding so the result is independent of
    input casing, matching ``abi.encode``.
    """
    return bytes(
        keccak(
            abi_encode(
                ["address", "address", "bytes32"],
                [
                    Web3.to_checksum_address(payer),
                    Web3.to_checksum_address(service),
                    salt,
                ],
            )
        )
    )
