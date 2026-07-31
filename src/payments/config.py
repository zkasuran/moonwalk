"""Payment layer configuration — Arc testnet + x402 constants.

Wallet model (two distinct parties so USDC actually moves):

  AGENT_PRIVATE_KEY       the payer. Holds testnet USDC, signs EIP-3009.
                          Needs USDC only — no native gas (facilitator pays gas).
  SELLER_WALLET_ADDRESS   the service (payTo). Receives the USDC.
  FACILITATOR_PRIVATE_KEY the relayer. Submits transferWithAuthorization on-chain
                          and pays gas. Usually the service operator's wallet.

For single-wallet local development, AGENT_PRIVATE_KEY and FACILITATOR_PRIVATE_KEY
fall back to the legacy DEPLOYER_PRIVATE_KEY (the wallet then pays itself).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# This lane's own .env is the source of truth, so it overrides ambient exports.
# Without override= a stale OPENAI_BASE_URL left in the shell silently redirects
# the agent's reasoning to a different endpoint than the one configured here.
load_dotenv(override=True)

# Arc testnet
ARC_NETWORK = os.getenv("ARC_NETWORK", "eip155:5042002")
ARC_CHAIN_ID = 5042002
ARC_RPC_URL = os.getenv("ARC_RPC_URL", "https://rpc.testnet.arc.network")

# Arc USDC contract (verified on-chain: name()="USDC", version()="2", decimals=6)
ARC_USDC_ADDRESS = "0x3600000000000000000000000000000000000000"
ARC_USDC_NAME = "USDC"
ARC_USDC_VERSION = "2"

# Legacy single-wallet key (deployer). Kept for backward compatibility / fallback.
DEPLOYER_PRIVATE_KEY = os.getenv("DEPLOYER_PRIVATE_KEY", "")

# Agent (payer) wallet. Falls back to the deployer key for single-wallet dev mode.
AGENT_PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "") or DEPLOYER_PRIVATE_KEY

# Facilitator / relayer wallet. Falls back to the deployer key.
FACILITATOR_PRIVATE_KEY = os.getenv("FACILITATOR_PRIVATE_KEY", "") or DEPLOYER_PRIVATE_KEY

# Service wallet that receives the USDC (payTo).
SELLER_WALLET_ADDRESS = os.getenv(
    "SELLER_WALLET_ADDRESS", "0x0000000000000000000000000000000000000000"
)

# Default command price: $0.01 USDC (10_000 atomic units, 6 decimals)
DEFAULT_PRICE_ATOMIC = int(os.getenv("DEFAULT_PRICE_ATOMIC", "10000"))

# Per-user spend budget the agent enforces before paying. Default $0.05.
DEFAULT_BUDGET_ATOMIC = int(os.getenv("DEFAULT_BUDGET_ATOMIC", "50000"))

# Anthropic (real /ask answers)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

# OpenAI-compatible provider (a gateway or self-hosted endpoint that speaks the
# OpenAI chat-completions protocol). When configured, the agent's reasoning runs
# here instead of Anthropic. OPENAI_BASE_URL should include the /v1 suffix.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")

# Force a provider: "openai" | "anthropic" | "" (auto: openai if configured, else
# anthropic). The agent's reasoning is free either way; USDC only pays for tools.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "")

# API server
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8402"))
API_BASE_URL = os.getenv("API_BASE_URL", f"http://localhost:{API_PORT}")

# Discord
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_APP_ID = os.getenv("DISCORD_APP_ID", "")
# Optional: a guild to sync slash commands to on startup. Guild-scoped syncs are
# instant, unlike global commands which can take up to an hour to appear.
GUILD_ID = os.getenv("GUILD_ID", "")

# SQLite DB path
DB_PATH = os.getenv("DB_PATH", "nanopay.db")

# ── Channel rail ─────────────────────────────────────────────────────────
# The channel is identified by (payer, service, salt), so a stable salt lets both
# sides derive the same channel id with no lookup and no shared state.
CHANNEL_SALT = os.getenv("MOONWALK_CHANNEL_SALT", "moonwalk:v1")

# Collect once this much has accrued off-chain. Lower means more transactions and
# tighter counterparty risk for the service, higher means cheaper per call.
REDEEM_THRESHOLD_ATOMIC = int(os.getenv("MOONWALK_REDEEM_THRESHOLD_ATOMIC", "20000"))

# The channel payer. The service only ever needs the payer's ADDRESS: it verifies
# voucher signatures against it and never holds the payer's key. Falls back to the
# address of AGENT_PRIVATE_KEY for single-machine development.
CHANNEL_PAYER_ADDRESS = os.getenv("MOONWALK_CHANNEL_PAYER", "")

# Per-subject caps applied when a channel is opened by scripts/open_channel.py.
CHANNEL_DEFAULT_CAP_ATOMIC = int(os.getenv("MOONWALK_DEFAULT_CAP_ATOMIC", "50000"))
CHANNEL_CAP_WINDOW_SECONDS = int(os.getenv("MOONWALK_CAP_WINDOW_SECONDS", "86400"))

# Which rail the agent prefers: "auto" tries the channel and falls back to x402,
# "channel" refuses to pay any other way, "x402" ignores the channel entirely.
RAIL_PREFERENCE = os.getenv("MOONWALK_RAIL", "auto").strip().lower()
