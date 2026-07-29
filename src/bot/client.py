"""Discord bot: the autonomous buyer.

The agent pays from its own wallet and never sends a transaction. Two rails do
that. On the channel it signs a voucher, which costs it nothing and settles later
in a batch. On x402 it signs an EIP-3009 authorization and the facilitator settles
that call on-chain immediately. It prefers the channel when the service offers one.

Commands:
  /ask prompt:<text>       — the agent decides what (if anything) to pay for
  /budget                  — show remaining USDC spend budget
  /channel                 — the nanopayment channel: deposit, meters, settlements
  /cap                     — admin sets a member's on-chain spend cap
  /price symbol:<ticker>   — direct live price (CoinGecko), $0.001
  /weather city:<city>     — direct live weather (Open-Meteo), $0.001
  /news topic:<topic>      — latest headlines (Google News), $0.001
  /gpt prompt:<text>       — direct premium answer (Claude), $0.01
  /sell                    — list your own priced service on the marketplace
  /verify-service          — admin approves a listing so the agent can buy it
  /services                — browse this server's marketplace
  /ping                    — payment smoke test
  /nanopay-info            — about the bot
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
import httpx
from discord import app_commands
from eth_account.signers.local import LocalAccount

from ..agent import planner
from ..agent.tools import TOOL_CATALOG, MarketToolSpec, ToolSpec, get_tool, market_tool
from ..chain import config as chain_config
from ..chain.channel import ChannelClient
from ..chain.client import ArcClient
from ..payments.config import (
    AGENT_PRIVATE_KEY,
    API_BASE_URL,
    DEFAULT_PRICE_ATOMIC,
    DISCORD_BOT_TOKEN,
    GUILD_ID,
    RAIL_PREFERENCE,
)
from .channel_payer import CapReached, ChannelUnavailable, pay_on_channel
from .payer import build_paying_client, pay_and_execute

logger = logging.getLogger("nanopay.bot")


# ============================================================================
# Bot client
# ============================================================================


class NanoPayBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # plain client for /payments/create and /status — no payment needed
        self._api: httpx.AsyncClient | None = None
        # x402-paying client — auto-handles 402 and retries with EIP-3009 sig
        self._payer: httpx.AsyncClient | None = None
        # channel rail: the account that signs vouchers and the contract client
        # that knows how to hash them. No RPC is made until a call needs one.
        self._payer_account: LocalAccount | None = None
        self._chain: ChannelClient | None = None

    async def _sync_guild(self, guild: discord.abc.Snowflake) -> None:
        """Sync commands to one guild only (instant), the single source of truth.

        Copying globals into a guild while ALSO having them registered globally
        makes Discord show every command twice, so we do not global-sync. The
        guild copy is authoritative and appears in seconds.
        """
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info("Synced %d commands to guild %s", len(synced), getattr(guild, "id", guild))

    async def setup_hook(self) -> None:
        self._api = httpx.AsyncClient(base_url=API_BASE_URL, timeout=30)
        self._payer = build_paying_client()
        if AGENT_PRIVATE_KEY and RAIL_PREFERENCE != "x402":
            self._payer_account = ArcClient.account(AGENT_PRIVATE_KEY)
            self._chain = ChannelClient(ArcClient())
            logger.info("channel rail available, payer %s", self._payer_account.address)
        if GUILD_ID:
            await self._sync_guild(discord.Object(id=int(GUILD_ID)))

    async def on_ready(self) -> None:
        logger.info("NanoPay bot ready as %s", self.user)
        for guild in self.guilds:
            await self._sync_guild(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        # A guild invited while the bot is running misses on_ready, so sync here
        # too, otherwise its commands do not appear.
        await self._sync_guild(guild)

    async def close(self) -> None:
        if self._api:
            await self._api.aclose()
        if self._payer:
            await self._payer.aclose()
        await super().close()


bot = NanoPayBot()


# ============================================================================
# Helpers
# ============================================================================


async def _create_payment(
    guild_id: str,
    channel_id: str,
    user_id: str,
    command_name: str,
    command_args: dict[str, str],
    interaction_token: str,
    application_id: str,
    price_atomic: int = DEFAULT_PRICE_ATOMIC,
    pay_to: str = "",
) -> dict[str, str]:
    assert bot._api is not None
    resp = await bot._api.post(
        "/payments/create",
        json={
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "command_name": command_name,
            "command_args": command_args,
            "price_atomic": price_atomic,
            "pay_to": pay_to,
            "interaction_token": interaction_token,
            "application_id": application_id,
        },
    )
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


async def _get_budget(user_id: str) -> dict[str, int]:
    assert bot._api is not None
    resp = await bot._api.get(f"/budget/{user_id}")
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


async def _guild_catalog(guild_id: str) -> list[ToolSpec]:
    """Builtins plus this guild's verified marketplace services.

    A marketplace outage never blocks /ask: on any error the agent just plans
    over the builtin catalog.
    """
    assert bot._api is not None
    try:
        resp = await bot._api.get(f"/market/services/{guild_id}")
        resp.raise_for_status()
        services = resp.json().get("services", [])
    except Exception as exc:
        logger.debug("market catalog fetch failed for guild %s: %s", guild_id, exc)
        return list(TOOL_CATALOG)
    extra: list[ToolSpec] = []
    for s in services:
        try:
            extra.append(
                market_tool(
                    name=str(s["name"]),
                    description=str(s.get("description", "")),
                    url=str(s.get("url", "")),
                    price_atomic=int(s["price_atomic"]),
                    wallet=str(s.get("wallet", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return list(TOOL_CATALOG) + extra


def _price_display(atomic: int) -> str:
    return f"${atomic / 1_000_000:.4f}"


def _tool_price(name: str) -> int:
    """Price for a catalog tool in atomic units, falling back to the default."""
    tool = get_tool(name)
    return tool.price_atomic if tool else DEFAULT_PRICE_ATOMIC


def _result_embed(
    command_name: str,
    result: str,
    tx: str,
    rail: str = "x402-exact",
    channel: dict[str, Any] | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"/{command_name}",
        description=result,
        color=0x22C55E,
    )
    if rail == "nanochannel" and channel is not None:
        # There is no per-call hash by design. The proof is the signed voucher now
        # and the batch transaction later, so say that instead of showing nothing.
        cumulative = _price_display(int(channel.get("cumulativeAtomic", 0)))
        embed.add_field(
            name="Paid",
            value=f"voucher signed, cumulative {cumulative}",
            inline=False,
        )
        embed.add_field(name="Calls on channel", value=str(channel.get("calls", "?")), inline=True)
        embed.add_field(
            name="Cap left",
            value=_price_display(int(channel.get("capRemainingAtomic", 0))),
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=f"[{str(channel.get('channelId', ''))[:14]}...]"
            f"({chain_config.address_url(chain_config.NANO_CHANNEL_ADDRESS)})",
            inline=False,
        )
        embed.set_footer(text="Metered on the MoonWalk channel, settles on Arc in a batch")
        return embed
    if tx:
        embed.add_field(
            name="Arc tx",
            value=f"[{tx[:16]}...]({chain_config.tx_url(tx)})",
            inline=False,
        )
    embed.set_footer(text="Paid per call via x402 on Arc testnet")
    return embed


async def _pay(payment_id: str, price_atomic: int) -> tuple[dict[str, Any], str]:
    """Pay for one call and return the service's response plus the rail used.

    The channel goes first when the service offers one: a voucher costs the agent
    a signature and no gas, where x402 costs a settlement per call. x402 stays the
    fallback, and it is still the right rail for a caller with no channel open.
    """
    if RAIL_PREFERENCE != "x402" and bot._chain and bot._payer_account and bot._api:
        try:
            data = await pay_on_channel(
                bot._api, bot._payer_account, bot._chain, payment_id, price_atomic
            )
            return data, "nanochannel"
        except CapReached:
            raise
        except ChannelUnavailable as exc:
            logger.debug("no channel offered: %s", exc)
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail the call
            logger.warning("channel payment failed, falling back to x402: %s", exc)
    if RAIL_PREFERENCE == "channel":
        raise RuntimeError("MOONWALK_RAIL=channel but the channel rail is unavailable")
    assert bot._payer is not None
    return await pay_and_execute(bot._payer, payment_id), "x402-exact"


async def _handle_premium_command(
    interaction: discord.Interaction,
    command_name: str,
    args: dict[str, str],
    price_atomic: int = DEFAULT_PRICE_ATOMIC,
) -> None:
    """Handle a premium slash command with autonomous x402 payment."""
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id or "dm")
    channel_id = str(interaction.channel_id or "")
    user_id = str(interaction.user.id)

    # Register payment record
    try:
        data = await _create_payment(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            command_name=command_name,
            command_args=args,
            interaction_token=interaction.token,
            application_id=str(interaction.application_id),
            price_atomic=price_atomic,
        )
    except Exception as exc:
        await interaction.followup.send(f"Could not initialise payment: {exc}", ephemeral=True)
        return

    payment_id = data["payment_id"]
    pay_url = data["pay_url"]
    price_str = _price_display(price_atomic)

    # Mode A: the agent pays autonomously, channel first then x402
    try:
        result_data, rail = await _pay(payment_id, price_atomic)
        await interaction.followup.send(
            embed=_result_embed(
                command_name,
                str(result_data.get("result", "(no result)")),
                str(result_data.get("tx_hash", "")),
                rail,
                result_data,
            ),
            ephemeral=True,
        )
        return
    except CapReached as exc:
        await interaction.followup.send(
            f"The contract will not let this call through: {exc}. "
            "A server admin can raise your cap with /cap.",
            ephemeral=True,
        )
        return
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 402:
            # Unexpected error — surface it
            await interaction.followup.send(
                f"Payment error ({exc.response.status_code}): {exc.response.text[:200]}",
                ephemeral=True,
            )
            return
        # 402 even after auto-pay attempt — fall through to Mode B
        logger.warning("Auto-pay failed (402 after retry), falling back to MetaMask: %s", exc)
    except Exception as exc:
        logger.warning("Auto-pay error, falling back to MetaMask: %s", exc)

    # Mode B: manual MetaMask fallback
    embed = discord.Embed(
        title=f"/{command_name} — pay {price_str} USDC",
        description=(
            "Bot wallet insufficient. Click below to pay via MetaMask on Arc Testnet.\n"
            "Result will appear here once confirmed."
        ),
        color=0xF59E0B,
    )
    embed.add_field(name="Amount", value=price_str, inline=True)
    embed.add_field(name="Network", value="Arc Testnet", inline=True)
    embed.set_footer(text="x402 EIP-3009 · NanoPay")

    view = _PayView(pay_url)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # Poll for manual payment completion
    asyncio.create_task(_poll_and_deliver(interaction, payment_id, command_name))


async def _handle_agent_request(interaction: discord.Interaction, prompt: str) -> None:
    """The agentic path: the agent decides whether to spend, on which tool, within budget."""
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)

    try:
        budget = await _get_budget(user_id)
    except Exception as exc:
        await interaction.followup.send(f"Could not read budget: {exc}", ephemeral=True)
        return
    remaining = budget["remaining_atomic"]

    guild_id = str(interaction.guild_id or "dm")
    catalog = await _guild_catalog(guild_id)
    try:
        decision = await planner.decide(prompt, remaining, catalog)
    except Exception as exc:
        logger.warning("agent decide failed: %s", exc)
        await interaction.followup.send(
            "The agent could not reach its model just now. Try again in a moment.",
            ephemeral=True,
        )
        return

    # No spend needed — the agent answers for free.
    if decision.action == "answer_free":
        try:
            answer = await planner.answer_free(prompt)
        except Exception as exc:
            logger.warning("agent answer_free failed: %s", exc)
            await interaction.followup.send(
                "The agent could not reach its model just now. Try again in a moment.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(title="Agent answer (free)", description=answer, color=0x60A5FA)
        embed.add_field(name="Decision", value=decision.reason, inline=False)
        embed.add_field(name="Spent", value="$0.0000", inline=True)
        embed.add_field(name="Budget left", value=_price_display(remaining), inline=True)
        embed.set_footer(text="NanoPay agent • no payment needed")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Over budget — the agent declines.
    if decision.action == "decline":
        embed = discord.Embed(
            title="Agent declined to spend", description=decision.reason, color=0xF59E0B
        )
        embed.add_field(name="Budget left", value=_price_display(remaining), inline=True)
        embed.set_footer(text="NanoPay agent • spend governance")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Pay: the agent buys the chosen tool over x402, then composes the answer.
    assert decision.tool is not None
    tool = decision.tool
    arg_val = decision.args.get(tool.arg_name) or prompt
    command_args = {tool.arg_name: arg_val}
    pay_to = ""
    if isinstance(tool, MarketToolSpec):
        # Marketplace buy: the market executor needs the endpoint, and the
        # settlement goes to the lister's wallet instead of the house service.
        command_args["url"] = tool.url
        command_args["service"] = tool.service_name
        pay_to = tool.wallet
    try:
        data = await _create_payment(
            guild_id=guild_id,
            channel_id=str(interaction.channel_id or ""),
            user_id=user_id,
            command_name=tool.command,
            command_args=command_args,
            interaction_token=interaction.token,
            application_id=str(interaction.application_id),
            price_atomic=tool.price_atomic,
            pay_to=pay_to,
        )
        result_data, rail = await _pay(data["payment_id"], tool.price_atomic)
    except CapReached as exc:
        await interaction.followup.send(
            f"The contract refused this spend: {exc}. An admin can raise the cap with /cap.",
            ephemeral=True,
        )
        return
    except Exception as exc:
        logger.warning("agent pay/execute failed: %s", exc)
        await interaction.followup.send(f"Agent payment failed: {exc}", ephemeral=True)
        return

    tx = str(result_data.get("tx_hash", ""))
    tool_result = str(result_data.get("result", "(no result)"))
    # The payment already settled, so never lose the paid result: if composing the
    # answer fails, fall back to the raw tool output the user paid for.
    try:
        final = await planner.compose(prompt, tool.name, tool_result)
    except Exception as exc:
        logger.warning("agent compose failed, using raw tool result: %s", exc)
        final = tool_result

    spent_after = budget["spent_atomic"] + tool.price_atomic
    left_after = max(0, budget["limit_atomic"] - spent_after)

    embed = discord.Embed(title="Agent answer", description=final, color=0x22C55E)
    embed.add_field(name="Decision", value=f"Paid {tool.name} ({tool.price_display})", inline=False)
    embed.add_field(name="Spent now", value=tool.price_display, inline=True)
    embed.add_field(name="Budget left", value=_price_display(left_after), inline=True)
    if rail == "nanochannel":
        embed.add_field(
            name="Rail",
            value=(
                "channel voucher, no transaction. Cumulative "
                f"{_price_display(int(result_data.get('cumulativeAtomic', 0)))} over "
                f"{result_data.get('calls', '?')} calls, cap left "
                f"{_price_display(int(result_data.get('capRemainingAtomic', 0)))}"
            ),
            inline=False,
        )
        embed.set_footer(text="Metered on the MoonWalk channel, settles on Arc in a batch")
    else:
        if tx:
            embed.add_field(
                name="Arc receipt",
                value=f"[{tx[:16]}...]({chain_config.tx_url(tx)})",
                inline=False,
            )
        embed.set_footer(text="Paid per call via x402 on Arc testnet")
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _poll_and_deliver(
    interaction: discord.Interaction,
    payment_id: str,
    command_name: str,
    max_seconds: int = 600,
) -> None:
    """Background task: poll until paid, then post result (MetaMask fallback)."""
    assert bot._api is not None
    deadline = asyncio.get_event_loop().time() + max_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await bot._api.get(f"/status/{payment_id}")
            d = resp.json()
            if d.get("status") == "paid":
                await interaction.followup.send(
                    embed=_result_embed(
                        command_name,
                        d.get("result", "(no result)"),
                        d.get("tx_hash", ""),
                    ),
                    ephemeral=True,
                )
                return
        except Exception as exc:
            logger.debug("Poll error: %s", exc)
        await asyncio.sleep(3)

    await interaction.followup.send(
        "Payment timed out. Run the command again to retry.",
        ephemeral=True,
    )


class _PayView(discord.ui.View):
    def __init__(self, pay_url: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Pay with MetaMask",
                url=pay_url,
                style=discord.ButtonStyle.link,
                emoji="💜",
            )
        )


# ============================================================================
# Slash commands
# ============================================================================


@bot.tree.command(name="weather", description="Current weather, $0.001 USDC via x402")
@app_commands.describe(city="City name")
async def cmd_weather(interaction: discord.Interaction, city: str) -> None:
    await _handle_premium_command(interaction, "weather", {"city": city}, _tool_price("weather"))


@bot.tree.command(name="price", description="Crypto price, $0.001 USDC via x402")
@app_commands.describe(symbol="Token symbol e.g. BTC, ETH")
async def cmd_price(interaction: discord.Interaction, symbol: str) -> None:
    await _handle_premium_command(
        interaction, "price", {"symbol": symbol}, _tool_price("crypto_price")
    )


@bot.tree.command(name="news", description="Latest news on any topic, $0.001 USDC via x402")
@app_commands.describe(topic="Topic, e.g. World Cup, Trump, Tesla")
async def cmd_news(interaction: discord.Interaction, topic: str) -> None:
    await _handle_premium_command(interaction, "news", {"topic": topic}, _tool_price("news"))


@bot.tree.command(name="ask", description="Ask the agent, it decides what (if anything) to pay for")
@app_commands.describe(prompt="Anything. The agent picks a paid tool only if it helps.")
async def cmd_ask(interaction: discord.Interaction, prompt: str) -> None:
    await _handle_agent_request(interaction, prompt)


@bot.tree.command(name="budget", description="Show your remaining USDC spend budget")
async def cmd_budget(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    budget = await _get_budget(str(interaction.user.id))
    embed = discord.Embed(title="Your NanoPay budget", color=0x7C3AED)
    embed.add_field(name="Limit", value=_price_display(budget["limit_atomic"]), inline=True)
    embed.add_field(name="Spent", value=_price_display(budget["spent_atomic"]), inline=True)
    embed.add_field(name="Left", value=_price_display(budget["remaining_atomic"]), inline=True)
    embed.set_footer(text="The agent will not spend past your limit.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="gpt", description="Direct premium answer, $0.01 USDC via x402")
@app_commands.describe(prompt="Your question")
async def cmd_gpt(interaction: discord.Interaction, prompt: str) -> None:
    await _handle_premium_command(
        interaction, "ask", {"prompt": prompt}, _tool_price("deep_answer")
    )


@bot.tree.command(name="ping", description="x402 smoke test, $0.001 USDC, bot pays")
async def cmd_ping(interaction: discord.Interaction) -> None:
    await _handle_premium_command(interaction, "ping", {}, 1000)


# ============================================================================
# Channel commands
# ============================================================================


@bot.tree.command(
    name="channel",
    description="The nanopayment channel: deposit, per-person meters and settlements",
)
async def cmd_channel(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    assert bot._api is not None
    try:
        resp = await bot._api.get("/channel")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Could not read the channel: {exc}", ephemeral=True)
        return

    contracts = data.get("contracts", {})
    if not data.get("enabled"):
        embed = discord.Embed(
            title="No channel open",
            description=str(data.get("reason", "the service has no channel right now")),
            color=0xF59E0B,
        )
        channel_url = chain_config.address_url(str(contracts.get("nanoChannel", "")))
        embed.add_field(
            name="Contracts on Arc",
            value=f"[NanoChannel]({channel_url})",
            inline=False,
        )
        embed.set_footer(text="Payments still work per call over x402")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    onchain = data.get("onchain", {})
    offchain = data.get("offchain", {})
    settlements = data.get("settlements", [])

    embed = discord.Embed(
        title="MoonWalk channel on Arc",
        description=(
            "The agent signs a voucher per call and never sends a transaction. "
            "The service collects the lot in one transaction when enough has accrued."
        ),
        color=0x7C3AED,
    )
    embed.add_field(
        name="Deposit", value=_price_display(int(onchain.get("depositAtomic", 0))), inline=True
    )
    embed.add_field(
        name="Collected", value=_price_display(int(onchain.get("redeemedAtomic", 0))), inline=True
    )
    embed.add_field(
        name="Left", value=_price_display(int(onchain.get("outstandingAtomic", 0))), inline=True
    )
    embed.add_field(name="Calls metered", value=str(offchain.get("meteredCalls", 0)), inline=True)
    embed.add_field(
        name="Waiting to settle",
        value=_price_display(int(offchain.get("pendingAtomic", 0))),
        inline=True,
    )
    embed.add_field(
        name="Settles at",
        value=_price_display(int(offchain.get("thresholdAtomic", 0))),
        inline=True,
    )

    mine = next(
        (
            s
            for s in offchain.get("subjects", [])
            if str(s.get("userId")) == str(interaction.user.id)
        ),
        None,
    )
    if mine is not None:
        embed.add_field(
            name="You",
            value=(
                f"{mine.get('calls', 0)} calls, "
                f"{_price_display(int(mine.get('cumulativeAtomic', 0)))} total, "
                f"cap left {_price_display(int(mine.get('capRemainingAtomic', 0)))}"
            ),
            inline=False,
        )

    if settlements:
        last = settlements[0]
        embed.add_field(
            name="Last settlement",
            value=(
                f"{last.get('calls', 0)} calls for "
                f"{_price_display(int(last.get('totalAtomic', 0)))} in one transaction "
                f"[{str(last.get('txHash', ''))[:14]}...]({last.get('url', '')})"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Channel {str(data.get('channelId', ''))[:18]}... on Arc testnet")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="cap",
    description="Admin: set a member's spend cap in the contract",
)
@app_commands.describe(
    member="Whose cap to set",
    limit="USDC allowed per window, e.g. 0.05",
    window_hours="Length of the window in hours. 0 means a lifetime cap.",
)
async def cmd_cap(
    interaction: discord.Interaction,
    member: discord.Member,
    limit: float,
    window_hours: float = 24.0,
) -> None:
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and (perms.administrator or perms.manage_guild)):
        await interaction.response.send_message(
            "Only a server admin can set a cap.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    assert bot._api is not None
    limit_atomic = int(round(limit * 1_000_000))
    window_seconds = int(round(window_hours * 3600))
    try:
        resp = await bot._api.post(
            "/channel/cap",
            json={
                "guild_id": str(interaction.guild_id or "dm"),
                "user_id": str(member.id),
                "limit_atomic": limit_atomic,
                "window_seconds": window_seconds,
            },
        )
        if resp.status_code != 200:
            detail = resp.json().get("detail", resp.text[:200])
            await interaction.followup.send(f"Could not set the cap: {detail}", ephemeral=True)
            return
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Could not set the cap: {exc}", ephemeral=True)
        return

    window_text = (
        "for their lifetime on this channel"
        if window_seconds == 0
        else (f"every {window_hours:g}h")
    )
    embed = discord.Embed(
        title=f"Cap set for {member.display_name}",
        description=(
            f"The contract will now refuse any spend above "
            f"{_price_display(limit_atomic)} {window_text}. Not the bot, the contract."
        ),
        color=0x22C55E,
    )
    embed.add_field(
        name="Transaction",
        value=f"[{str(data.get('txHash', ''))[:16]}...]({data.get('url', '')})",
        inline=False,
    )
    cap = data.get("cap", {})
    embed.add_field(
        name="Available now",
        value=_price_display(int(cap.get("remainingAtomic", 0))),
        inline=True,
    )
    embed.set_footer(text="SpendGuard on Arc testnet")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================================
# Marketplace commands
# ============================================================================


@bot.tree.command(
    name="sell",
    description="List your own priced service on this server's marketplace",
)
@app_commands.describe(
    name="Service name (a-z, 0-9, _)",
    url="Public http(s) endpoint; the agent calls it with ?q=<request>",
    price="Price per call in USDC, e.g. 0.001 (max 0.01)",
    wallet="Your 0x wallet that receives the USDC",
    description="What the service answers, so the agent knows when to buy it",
)
async def cmd_sell(
    interaction: discord.Interaction,
    name: str,
    url: str,
    price: float,
    wallet: str,
    description: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    assert bot._api is not None
    resp = await bot._api.post(
        "/market/list",
        json={
            "guild_id": str(interaction.guild_id or "dm"),
            "lister_id": str(interaction.user.id),
            "name": name.strip().lower(),
            "url": url,
            "price_atomic": int(round(price * 1_000_000)),
            "wallet": wallet,
            "description": description,
        },
    )
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text[:200])
        await interaction.followup.send(f"Listing rejected: {detail}", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"Listed: {name.strip().lower()}",
        description=(
            "Your service is on this server's marketplace, pending admin review. "
            "Once an admin runs `/verify-service`, the agent can discover it and "
            "pay your wallet per call."
        ),
        color=0xF59E0B,
    )
    embed.add_field(name="Price", value=f"${price:.4f} USDC per call", inline=True)
    embed.add_field(name="Pays to", value=f"`{wallet[:10]}…`", inline=True)
    embed.set_footer(text="NanoPay marketplace • awaiting verification")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="verify-service",
    description="Admin: approve a marketplace listing so the agent can buy it",
)
@app_commands.describe(name="The listed service name to approve")
async def cmd_verify_service(interaction: discord.Interaction, name: str) -> None:
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and (perms.administrator or perms.manage_guild)):
        await interaction.response.send_message(
            "Only a server admin can verify a listing.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    assert bot._api is not None
    resp = await bot._api.post(
        "/market/verify",
        json={
            "guild_id": str(interaction.guild_id or "dm"),
            "name": name.strip().lower(),
            "admin_id": str(interaction.user.id),
        },
    )
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text[:200])
        await interaction.followup.send(f"Could not verify: {detail}", ephemeral=True)
        return
    await interaction.followup.send(
        f"`{name.strip().lower()}` is verified. The agent can now discover it in "
        "/ask and pay the lister's wallet per call.",
        ephemeral=True,
    )


@bot.tree.command(
    name="services",
    description="Browse this server's marketplace: what the agent can buy here",
)
async def cmd_services(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    assert bot._api is not None
    resp = await bot._api.get(f"/market/services/{interaction.guild_id or 'dm'}?all=true")
    resp.raise_for_status()
    services = resp.json().get("services", [])
    embed = discord.Embed(title="This server's marketplace", color=0x7C3AED)
    if not services:
        embed.description = (
            "No services listed yet. Any member can list one with `/sell`: "
            "a public endpoint, a sub-cent price and the wallet that gets paid."
        )
    else:
        for s in services[:12]:
            status = "verified, agent can buy it" if s.get("verified") else "pending admin review"
            embed.add_field(
                name=f"{s['name']} · {s['price_usdc']}",
                value=f"{s.get('description') or 'no description'}\n_{status}_",
                inline=False,
            )
    embed.set_footer(text="NanoPay marketplace • the agent pays listers directly")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================================
# Info command
# ============================================================================


@bot.tree.command(name="nanopay-info", description="About MoonWalk and the NanoPay agent")
async def cmd_info(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="MoonWalk, the agent that pays its own way",
        description=(
            "Ask for something behind a paywall. The agent decides whether it is "
            "worth buying, pays in USDC on Arc from its own wallet, and returns the "
            "result. It never sends a transaction and no human signs anything.\n\n"
            "Two rails. On the channel it signs a voucher per call, and the service "
            "collects many calls in one transaction. On x402 it signs an EIP-3009 "
            "authorization and that single call settles on-chain immediately."
        ),
        color=0x7C3AED,
    )
    embed.add_field(name="Network", value="Arc testnet (eip155:5042002)", inline=False)
    embed.add_field(
        name="Your limit",
        value=(
            "Each person has their own cap held in the SpendGuard contract, so the "
            "operator cannot spend past it either. Check yours with /channel."
        ),
        inline=False,
    )
    embed.add_field(
        name="Contracts",
        value=(
            f"[NanoChannel]({chain_config.address_url(chain_config.NANO_CHANNEL_ADDRESS)}) · "
            f"[SpendGuard]({chain_config.address_url(chain_config.SPEND_GUARD_ADDRESS)}) · "
            f"[ServiceRegistry]({chain_config.address_url(chain_config.SERVICE_REGISTRY_ADDRESS)})"
        ),
        inline=False,
    )
    embed.add_field(
        name="Pricing",
        value="$0.001 per data call, $0.01 for a premium answer",
        inline=False,
    )
    embed.set_footer(text="MoonWalk, Programmable Money Hackathon 2026")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================================
# Entry point
# ============================================================================


def run() -> None:
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN not set. Add it to .env")
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(DISCORD_BOT_TOKEN)
