"""Open the production channel on Arc and set its per-person caps.

Idempotent. Run it once for a deployment, or again after a close: if the channel
already exists it prints the state and stops. The payer signs the deposit, the
service submits it and pays the gas, and the service also becomes the cap owner,
which is what lets the payer stay a pure signer with no transactions.

    .venv/bin/python scripts/open_channel.py 0.50
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_utils.crypto import keccak  # noqa: E402

from src.chain import ArcClient, ChannelClient, GuardClient  # noqa: E402
from src.chain import config as chain_config  # noqa: E402
from src.payments import config as pay_config  # noqa: E402


def main() -> int:
    deposit_usdc = float(sys.argv[1]) if len(sys.argv) > 1 else 0.50
    deposit = int(round(deposit_usdc * 1_000_000))

    agent_key = os.getenv("AGENT_PRIVATE_KEY", "")
    service_key = os.getenv("FACILITATOR_PRIVATE_KEY", "") or os.getenv("DEPLOYER_PRIVATE_KEY", "")
    if not agent_key or not service_key:
        print("need AGENT_PRIVATE_KEY and FACILITATOR_PRIVATE_KEY (or DEPLOYER_PRIVATE_KEY)")
        return 2

    client = ArcClient()
    client.assert_arc()
    chain = ChannelClient(client)
    guard = GuardClient(client)
    payer = ArcClient.account(agent_key)
    service = ArcClient.account(service_key)
    service_address = pay_config.SELLER_WALLET_ADDRESS or service.address

    salt = keccak(text=pay_config.CHANNEL_SALT)
    channel_id = ChannelClient.channel_id_local(payer.address, service_address, salt)
    onchain_id = chain.channel_id(payer.address, service_address, salt)
    assert channel_id == onchain_id, "local channel id derivation disagrees with the contract"

    print(f"channel   0x{channel_id.hex()}")
    print(f"payer     {payer.address}")
    print(f"service   {service_address}")
    print(f"salt      {pay_config.CHANNEL_SALT}")

    state = chain.state(channel_id)
    if state.payer != "0x0000000000000000000000000000000000000000":
        print("\nalready open")
        print(f"  deposit     {state.deposit / 1e6:.6f} USDC")
        print(f"  redeemed    {state.redeemed / 1e6:.6f} USDC")
        print(f"  outstanding {state.outstanding / 1e6:.6f} USDC")
        print(f"  guarded     {state.guarded}, closing {state.closing}, settled {state.settled}")
        if state.settled:
            print("\nthis channel is closed. Set MOONWALK_CHANNEL_SALT to a new value and rerun.")
            return 1
        return 0

    balance = client.usdc_balance(payer.address)
    if balance < deposit:
        print(f"\npayer holds {balance / 1e6:.6f} USDC, needs {deposit / 1e6:.6f}")
        return 1

    print(f"\nopening with {deposit / 1e6:.6f} USDC")
    auth = chain.sign_deposit(payer, deposit)
    _, sent = chain.open(
        service,
        service_address,
        salt,
        guarded=True,
        auth=auth,
        cap_owner=service.address,
    )
    print(f"  open      {sent.url} (status {sent.status}, gas {sent.gas_used})")

    cap = pay_config.CHANNEL_DEFAULT_CAP_ATOMIC
    window = pay_config.CHANNEL_CAP_WINDOW_SECONDS
    capped = guard.set_default_cap(service, channel_id, cap, window)
    print(f"  cap       {cap / 1e6:.6f} USDC per {window}s per person: {capped.url}")

    print("\nready. The service can now meter calls off-chain and redeem in batches.")
    print(f"  explorer  {chain_config.address_url(chain_config.NANO_CHANNEL_ADDRESS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
