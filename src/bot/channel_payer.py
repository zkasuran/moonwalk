"""Payer side of the channel: read the offer, sign a voucher, retry.

Same shape as the x402 client the bot already uses. The difference is what the
payer sends back. x402 sends one EIP-3009 authorization per call, which settles
on-chain immediately. Here the payer sends a voucher, which costs it a signature
and nothing else: no transaction, no gas, no wait for a block.

The service's 402 carries the cumulative to sign, so the payer does not track
state. It only has to check the offer is for the channel and the person it thinks
it is paying for.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx
from eth_account.signers.local import LocalAccount

from ..chain.channel import ChannelClient, Voucher, voucher_to_dict

logger = logging.getLogger("moonwalk.bot.channel")

CHANNEL_REQUIRED_HEADER = "X-CHANNEL-REQUIRED"
CHANNEL_VOUCHER_HEADER = "X-CHANNEL-VOUCHER"


class ChannelUnavailable(Exception):
    """The service did not offer the channel rail for this call."""


class CapReached(Exception):
    """The offer says this person has no room left under their on-chain cap."""


def _bytes32(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


async def pay_on_channel(
    api: httpx.AsyncClient,
    payer: LocalAccount,
    chain: ChannelClient,
    payment_id: str,
    price_atomic: int,
) -> dict[str, Any]:
    """Meter one call on the channel. Returns the service's JSON response.

    Raises ChannelUnavailable if the service has no open channel, so the caller
    can fall back to paying per call over x402.
    """
    probe = await api.post(f"/execute/{payment_id}")
    if probe.status_code != 402:
        probe.raise_for_status()
        return dict(probe.json())

    offer_raw = probe.headers.get(CHANNEL_REQUIRED_HEADER) or probe.headers.get(
        CHANNEL_REQUIRED_HEADER.lower()
    )
    if not offer_raw:
        raise ChannelUnavailable("service offered no channel for this call")

    offer = json.loads(base64.b64decode(offer_raw))
    cap_left = int(offer.get("capRemainingAtomic", 0))
    if cap_left < price_atomic:
        raise CapReached(f"on-chain cap leaves {cap_left} atomic, this call needs {price_atomic}")

    voucher = Voucher(
        channel_id=_bytes32(str(offer["channelId"])),
        subject=_bytes32(str(offer["subject"])),
        cumulative=int(offer["cumulative"]),
        valid_before=int(offer["validBefore"]),
    )
    signature = chain.sign_voucher(payer, voucher)
    header = base64.b64encode(json.dumps(voucher_to_dict(voucher, signature)).encode()).decode()

    resp = await api.post(f"/execute/{payment_id}", headers={CHANNEL_VOUCHER_HEADER: header})
    resp.raise_for_status()
    logger.info(
        "metered %s atomic on channel %s, cumulative %s",
        price_atomic,
        offer["channelId"][:14],
        voucher.cumulative,
    )
    return dict(resp.json())
