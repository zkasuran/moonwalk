"""End to end proof of the channel rail through the real API.

Starts nothing. Point it at a running MoonWalk API (`make api`) and it will:

  1. create a payment record per call, the same way the Discord bot does
  2. pay each one on the channel: read the 402 offer, sign a voucher, retry
  3. show the per-person meters the service is holding
  4. force a settlement and check the contract agrees, subject by subject

Two people, six calls, one transaction. Writes evidence/rail-<timestamp>.json.

    make api                       # in another terminal
    .venv/bin/python scripts/rail_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.bot.channel_payer import CapReached, ChannelUnavailable, pay_on_channel  # noqa: E402
from src.chain import ArcClient, ChannelClient, discord_subject  # noqa: E402
from src.chain import config as chain_config  # noqa: E402

BASE = os.getenv("API_BASE_URL", "http://localhost:8402")
GUILD = "1517400111699726488"
PEOPLE = [("900000000000000001", 4), ("900000000000000002", 2)]
PRICE = 1_000  # $0.001 per call


def usd(atomic: int) -> str:
    return f"${atomic / 1_000_000:.6f}".rstrip("0").rstrip(".")


async def main() -> int:
    agent_key = os.getenv("AGENT_PRIVATE_KEY", "")
    if not agent_key:
        print("need AGENT_PRIVATE_KEY, the wallet that signs vouchers")
        return 2

    client = ArcClient()
    chain = ChannelClient(client)
    payer = ArcClient.account(agent_key)
    evidence: dict[str, Any] = {"api": BASE, "calls": [], "network": "arc-testnet"}

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as api:
        health = await api.get("/channel")
        state = health.json()
        if not state.get("enabled"):
            print(f"the API has no channel open: {state.get('reason')}")
            return 1
        channel_id = bytes.fromhex(str(state["channelId"])[2:])
        print(f"channel  {state['channelId']}")
        print(f"deposit  {usd(int(state['onchain']['depositAtomic']))}")
        print(f"left     {usd(int(state['onchain']['outstandingAtomic']))}")
        print(f"payer    {payer.address} (tx count {client.tx_count(payer.address)})")

        print("\nmetering calls, one signed voucher each")
        for user_id, calls in PEOPLE:
            for i in range(calls):
                created = await api.post(
                    "/payments/create",
                    json={
                        "guild_id": GUILD,
                        "channel_id": "rail-demo",
                        "user_id": user_id,
                        "command_name": "ping",
                        "command_args": {},
                        "price_atomic": PRICE,
                    },
                )
                created.raise_for_status()
                payment_id = created.json()["payment_id"]
                try:
                    data = await pay_on_channel(api, payer, chain, payment_id, PRICE)
                except CapReached as exc:
                    print(f"  user {user_id[-4:]} call {i + 1}: refused, {exc}")
                    evidence["calls"].append({"user": user_id, "refused": str(exc)})
                    continue
                except ChannelUnavailable as exc:
                    print(f"  the API stopped offering the channel: {exc}")
                    return 1
                print(
                    f"  user {user_id[-4:]} call {i + 1}: "
                    f"cumulative {usd(int(data['cumulativeAtomic']))}, "
                    f"cap left {usd(int(data['capRemainingAtomic']))}"
                )
                evidence["calls"].append(
                    {
                        "user": user_id,
                        "cumulativeAtomic": data["cumulativeAtomic"],
                        "callsOnChannel": data["calls"],
                        "rail": data["rail"],
                    }
                )

        before = (await api.get("/channel")).json()
        print(
            f"\nheld off-chain: {usd(int(before['offchain']['pendingAtomic']))}"
            f" over {before['offchain']['meteredCalls']} calls"
        )

        print("\nsettling")
        settled = await api.post("/channel/settle", params={"force": True})
        settled.raise_for_status()
        result = settled.json()
        if not result.get("settled"):
            print(f"  nothing settled: {result.get('reason')}")
            return 1
        print(f"  tx      {result['url']}")
        print(f"  total   {usd(int(result['totalAtomic']))} over {result['calls']} calls")
        print(f"  gas     {usd(int(result['gasFeeAtomic']))}")
        evidence["settlement"] = result

        print("\nchecking the contract agrees, per person")
        for user_id, _ in PEOPLE:
            subject = discord_subject(GUILD, user_id)
            onchain = chain.subject_redeemed(channel_id, subject)
            print(f"  user {user_id[-4:]}: contract says {usd(onchain)} settled")
            evidence.setdefault("onchainPerSubject", {})[user_id] = onchain

        after = (await api.get("/channel")).json()
        print(f"\npayer tx count still {client.tx_count(payer.address)}")
        print(f"channel left {usd(int(after['onchain']['outstandingAtomic']))}")
        evidence["after"] = after["onchain"]

    out = Path(__file__).resolve().parents[1] / "evidence"
    out.mkdir(exist_ok=True)
    path = out / f"rail-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"\nevidence {path.name}")
    print(f"explorer {chain_config.address_url(chain_config.NANO_CHANNEL_ADDRESS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
