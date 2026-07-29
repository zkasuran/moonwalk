"""Publish the priced catalog on-chain, then verify it.

The agent's tool list stops being a row in our database and becomes a public fact:
anyone can read what a service costs, who gets paid and who approved it. Run once
per Discord server.

  .venv/bin/python scripts/registry_seed.py            # show the catalog
  .venv/bin/python scripts/registry_seed.py --write    # claim, register, verify

Only endpoints that genuinely answer requests belong here. The contract rejects
anything that is not https, and a price change drops the verification, so an admin
only ever approves a price they saw.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.chain import ArcClient, RegistryClient  # noqa: E402
from src.chain import config as chain_config  # noqa: E402
from src.chain.registry import discord_namespace  # noqa: E402

GUILD = os.getenv("GUILD_ID", "1416577435369214084")

# name, description, endpoint, price in atomic USDC
LISTINGS = [
    (
        "moonwalk_ask",
        "Ask the MoonWalk agent. It decides whether to buy a priced tool and answers.",
        "https://nanopay-api.loadline.xyz/demo/ask",
        1_000,
    ),
]


def main() -> int:
    write = "--write" in sys.argv
    client = ArcClient()
    client.assert_arc()
    registry = RegistryClient(client)
    namespace = discord_namespace(GUILD)

    print(f"registry   {chain_config.SERVICE_REGISTRY_ADDRESS}")
    print(f"guild      {GUILD}")
    print(f"namespace  0x{namespace.hex()}")
    admin_now = registry.namespace_admin(namespace)
    print(f"admin      {admin_now}")

    if not write:
        for listing in registry.catalog(namespace, buyable_only=False):
            state = "buyable" if listing.buyable else "not verified"
            print(f"  {listing.name:<16} {listing.price_display:<10} {state}  {listing.endpoint}")
        if not registry.ids_of(namespace):
            print("  (nothing listed yet, rerun with --write)")
        return 0

    key = os.getenv("FACILITATOR_PRIVATE_KEY", "") or os.getenv("DEPLOYER_PRIVATE_KEY", "")
    if not key:
        print("need FACILITATOR_PRIVATE_KEY or DEPLOYER_PRIVATE_KEY to write")
        return 2
    admin = ArcClient.account(key)

    if admin_now == "0x0000000000000000000000000000000000000000":
        sent = registry.claim_namespace(admin, namespace)
        print(f"claimed    {sent.url}")
    elif admin_now.lower() != admin.address.lower():
        print(f"namespace belongs to {admin_now}, not {admin.address}")
        return 1

    for name, description, endpoint, price in LISTINGS:
        service_id = registry.service_id(namespace, name)
        try:
            existing = registry.get(service_id)
            print(f"exists     {name} at {existing.price_display}, buyable {existing.buyable}")
        except Exception:
            _, sent = registry.register(
                admin, namespace, name, description, endpoint, admin.address, price
            )
            print(f"listed     {name}  {sent.url}")
        if not registry.is_buyable(service_id):
            sent = registry.set_verified(admin, service_id, True)
            print(f"verified   {name}  {sent.url}")

    print("\ncatalog now on-chain:")
    for listing in registry.catalog(namespace, buyable_only=False):
        state = "buyable" if listing.buyable else "not verified"
        print(f"  {listing.name:<16} {listing.price_display:<10} {state}  {listing.endpoint}")
    print(f"\nexplorer   {chain_config.address_url(chain_config.SERVICE_REGISTRY_ADDRESS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
