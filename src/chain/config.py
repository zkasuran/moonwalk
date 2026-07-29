"""Arc chain configuration for the MoonWalk contracts.

Addresses default to the live Arc testnet deployment, the same one the receipts in
`deployments/arc-testnet.json` and the demo scripts point at, so a fresh clone
talks to real contracts with no setup. Override any of them to run against your
own deploy.

Amounts everywhere in this package are USDC atomic units, the 6 decimal ERC-20
view. Arc's native interface reports the same balance with 18 decimals, and
mixing the two is the classic Arc bug, so the native view never appears here.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

ARC_RPC_URL = os.getenv("ARC_RPC_URL", "https://rpc.testnet.arc.network")
ARC_CHAIN_ID = int(os.getenv("ARC_CHAIN_ID", "5042002"))
ARC_EXPLORER = os.getenv("ARC_EXPLORER", "https://testnet.arcscan.app").rstrip("/")

USDC_ADDRESS = os.getenv("ARC_USDC_ADDRESS", "0x3600000000000000000000000000000000000000")
USDC_DOMAIN_NAME = os.getenv("ARC_USDC_NAME", "USDC")
USDC_DOMAIN_VERSION = os.getenv("ARC_USDC_VERSION", "2")

# Deployed 2026-07-29 from contracts/script/Deploy.s.sol. Receipts and the
# compiler settings are in deployments/arc-testnet.json.
NANO_CHANNEL_ADDRESS = os.getenv(
    "MOONWALK_NANO_CHANNEL", "0x3e2dE84eD534E39241682957d617ed761892D568"
)
SPEND_GUARD_ADDRESS = os.getenv(
    "MOONWALK_SPEND_GUARD", "0xfbB8e1E61e8FbB09e5d5be308ac4F54D2865B67b"
)
SERVICE_REGISTRY_ADDRESS = os.getenv(
    "MOONWALK_SERVICE_REGISTRY", "0x774E5F27b572450F5D21FE3929B45557F3468F9b"
)

# EIP-712 domain of the channel contract. Must match NanoChannel's constants.
CHANNEL_DOMAIN_NAME = "MoonWalk NanoChannel"
CHANNEL_DOMAIN_VERSION = "1"

# How long a voucher stays redeemable. Long enough that a service can batch, short
# enough that a leaked voucher does not haunt the channel forever.
VOUCHER_TTL_SECONDS = int(os.getenv("MOONWALK_VOUCHER_TTL", "86400"))


def tx_url(tx_hash: str) -> str:
    """Explorer link for a transaction. Arcscan rejects a hash without 0x."""
    h = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    return f"{ARC_EXPLORER}/tx/{h}"


def address_url(address: str) -> str:
    return f"{ARC_EXPLORER}/address/{address}"
