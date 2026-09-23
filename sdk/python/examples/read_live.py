"""Read the live MoonWalk NanoChannel on Arc mainnet.

This talks to the real chain. It confirms the RPC is Arc mainnet, reads the
contract's challenge window, then reads outstanding and a subject's redeemed
total for one channel. Pass a real channel id and a Discord guild and user to
inspect a specific channel:

    CHANNEL_ID=0x... GUILD_ID=123 USER_ID=456 python examples/read_live.py

With no CHANNEL_ID it derives a sample id from a payer, a service and a salt.
That channel most likely does not exist yet, so its reads come back as zero,
which is itself a truthful read of the chain.
"""

from __future__ import annotations

import os

from moonwalk_nanopay import (
    ARC_CHAIN_ID,
    ARC_EXPLORER_URL,
    MAINNET_ADDRESSES,
    USDC_DECIMALS,
    NanopayClient,
)


def usdc(atomic: int) -> str:
    return f"{atomic / 10**USDC_DECIMALS:.6f} USDC"


def main() -> None:
    client = NanopayClient()
    client.assert_arc()
    print(f"Arc mainnet, chain id {ARC_CHAIN_ID}")
    print(f"NanoChannel {MAINNET_ADDRESSES.nano_channel}")
    print(f"{ARC_EXPLORER_URL}/address/{MAINNET_ADDRESSES.nano_channel}\n")

    print(f"challengeWindow: {client.challenge_window()} seconds")

    guild_id = os.getenv("GUILD_ID", "111111111111111111")
    user_id = os.getenv("USER_ID", "222222222222222222")
    subject = client.subject_id(guild_id, user_id)

    channel_id_env = os.getenv("CHANNEL_ID")
    if channel_id_env:
        hex_id = channel_id_env[2:] if channel_id_env.startswith("0x") else channel_id_env
        cid = bytes.fromhex(hex_id)
    else:
        cid = client.channel_id(
            "0xdb6c6340342e71a63cd11ebac2185204b7777777",
            "0x00000000000000000000000000000000000000AA",
            bytes.fromhex("00" * 31 + "01"),
        )

    print(f"\nchannelId: 0x{cid.hex()}")
    print(f"subject discord:{guild_id}:{user_id} -> 0x{subject.hex()}")

    state = client.channel_of(cid)
    if not state.exists:
        print("\nThis channel does not exist on chain yet. Reads below are zero.")
    else:
        print(f"\npayer {state.payer}")
        print(f"service {state.service}")
        print(f"deposit {usdc(state.deposit)}")
        print(f"redeemed {usdc(state.redeemed)}")
        print(f"guarded {state.guarded}")

    print(f"\noutstanding: {usdc(client.outstanding(cid))}")
    print(f"subjectRedeemed: {usdc(client.subject_redeemed(cid, subject))}")

    if state.guarded:
        cap = client.cap_of(cid, subject)
        if cap.configured:
            window = "lifetime" if cap.window == 0 else f"{cap.window}s"
            print(f"cap: {usdc(cap.limit)} per {window}")
            print(f"remaining: {usdc(client.remaining(cid, subject))}")


if __name__ == "__main__":
    main()
