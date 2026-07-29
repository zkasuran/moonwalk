"""FastAPI application: x402 payment endpoints + command execution backend.

Two payment rails share one endpoint. `/execute/{id}` answers 402 with both
offers: the x402 exact scheme (one EIP-3009 authorization per call, settled
immediately) and the MoonWalk channel (one signed voucher per call, settled in
batches). The caller picks by which header it sends back.

Routes:
  GET  /pay/{payment_id}      — HTML payment page (MetaMask or wallet link)
  POST /execute/{payment_id}  — Run the command once payment is proven
  GET  /status/{payment_id}   — Payment status (polled by bot)
  GET  /supported             — x402 facilitator supported schemes
  GET  /health                — Health check
  POST /market/list           — Member lists a priced service (unverified)
  POST /market/verify         — Admin verifies a listing so the agent can buy it
  GET  /market/services/{gid} — The per-guild marketplace catalog
  GET  /channel               — Channel state, per-person meters and settlements
  GET  /channel/quote         — The cumulative a payer should sign next
  POST /channel/settle        — Redeem the accrued vouchers now
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from x402.http.constants import PAYMENT_REQUIRED_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import (
    decode_payment_signature_header,
    encode_payment_required_header,
)
from x402.schemas import PaymentRequired, PaymentRequirements

from ..chain import config as chain_config
from ..chain.channel import voucher_from_dict
from ..domain.models import MarketplaceService, PaymentRecord, PaymentStatus
from ..payments.channel_rail import ChannelRail, build_rail
from ..payments.config import (
    API_BASE_URL,
    ARC_NETWORK,
    ARC_USDC_ADDRESS,
    ARC_USDC_NAME,
    ARC_USDC_VERSION,
    DB_PATH,
    DEFAULT_BUDGET_ATOMIC,
    DEFAULT_PRICE_ATOMIC,
    SELLER_WALLET_ADDRESS,
)
from ..payments.facilitator import EmbeddedFacilitatorClient, build_facilitator
from ..payments.store import PaymentStore
from .executor import execute_command
from .paywall import build_payment_page

logger = logging.getLogger("nanopay.api")

# The channel rail's two headers, shaped after x402's own: the server advertises
# what to sign, the client sends back the signed voucher.
CHANNEL_REQUIRED_HEADER = "X-CHANNEL-REQUIRED"
CHANNEL_VOUCHER_HEADER = "X-CHANNEL-VOUCHER"


# ============================================================================
# App state (populated in lifespan)
# ============================================================================

store: PaymentStore
facilitator_client: EmbeddedFacilitatorClient
rail: ChannelRail | None = None
_settle_lock = asyncio.Lock()
# Background settlement tasks, held so the loop cannot garbage collect them.
_background: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global store, facilitator_client, rail
    store = PaymentStore(DB_PATH)
    await store.init()

    fac = build_facilitator()
    facilitator_client = EmbeddedFacilitatorClient(fac)

    rail = build_rail(store)
    if rail is not None:
        logger.info("channel rail ready on %s", rail.channel_id_hex)
    else:
        logger.info("channel rail off, running the per-call x402 rail only")

    logger.info("MoonWalk API ready on Arc %s", ARC_NETWORK)
    yield
    logger.info("MoonWalk API shutting down")


app = FastAPI(
    title="MoonWalk: nanopayments for agent commerce on Arc", version="0.2.0", lifespan=lifespan
)

# The public landing page (GitHub Pages) calls /demo/ask from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- public demo throttle: protect the agent wallet from a runaway browser ----
DEMO_REQUEST_BUDGET_ATOMIC = 20_000  # $0.02 budget the agent sees per demo call
DEMO_TOTAL_CAP_ATOMIC = 300_000  # $0.30 total the demo will ever spend, then free-only
DEMO_MAX_PER_MIN = 5  # per-IP requests per rolling minute
_demo_hits: dict[str, list[float]] = {}
_demo_spent = {"atomic": 0}


def _demo_rate_ok(ip: str, now: float) -> bool:
    """True if this IP is under the per-minute limit; records the hit if so."""
    hits = [t for t in _demo_hits.get(ip, []) if now - t < 60.0]
    if len(hits) >= DEMO_MAX_PER_MIN:
        _demo_hits[ip] = hits
        return False
    hits.append(now)
    _demo_hits[ip] = hits
    return True


# ============================================================================
# Helpers
# ============================================================================


def _build_requirements(price_atomic: int, pay_to: str = "") -> PaymentRequirements:
    return PaymentRequirements.model_validate(
        {
            "scheme": "exact",
            "network": ARC_NETWORK,
            "asset": ARC_USDC_ADDRESS,
            "amount": str(price_atomic),
            "payTo": pay_to or SELLER_WALLET_ADDRESS,
            "maxTimeoutSeconds": 60,
            "extra": {
                "name": ARC_USDC_NAME,
                "version": ARC_USDC_VERSION,
            },
        }
    )


def _402_response(reqs: PaymentRequirements, channel: dict[str, Any] | None = None) -> JSONResponse:
    """Return a proper x402 v2 response, plus the channel offer when one is open.

    Requirements go in the PAYMENT-REQUIRED header. A client that speaks only
    x402 ignores the extra header and pays per call, exactly as before.
    """
    pr = PaymentRequired(accepts=[reqs])
    headers = {PAYMENT_REQUIRED_HEADER: encode_payment_required_header(pr)}
    rails = ["x402-exact"]
    if channel is not None:
        headers[CHANNEL_REQUIRED_HEADER] = base64.b64encode(json.dumps(channel).encode()).decode()
        rails.append("nanochannel")
    return JSONResponse(
        status_code=402,
        content={"x402Version": 2, "error": "payment required", "rails": rails},
        headers=headers,
    )


async def _channel_offer(record: PaymentRecord, price_atomic: int) -> dict[str, Any] | None:
    """What the payer would have to sign to meter this call on the channel."""
    if rail is None or not record.guild_id or not record.user_id:
        return None
    try:
        quote = await rail.quote(record.guild_id, record.user_id, price_atomic)
    except Exception as exc:  # noqa: BLE001 - the x402 rail must still be offered
        logger.warning("channel quote failed: %s", exc)
        return None
    return quote.as_dict()


async def _settle_when_due() -> None:
    """Collect in the background once enough has accrued.

    Kept off the request path so a user waiting on an answer never waits on a
    settlement, and behind a lock so two calls cannot redeem the same vouchers.
    """
    if rail is None:
        return
    async with _settle_lock:
        try:
            settlement = await rail.settle()
        except Exception as exc:  # noqa: BLE001
            logger.error("auto settle failed: %s", exc)
            return
    if settlement is not None:
        logger.info(
            "auto settled %s atomic over %s calls in %s",
            settlement.total_atomic,
            settlement.calls,
            settlement.tx_hash,
        )


# ============================================================================
# Routes
# ============================================================================


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "network": ARC_NETWORK}


@app.get("/pay/{payment_id}", response_class=HTMLResponse)
async def payment_page(payment_id: str) -> HTMLResponse:
    record = await store.get_payment(payment_id)
    if record is None:
        raise HTTPException(404, "Payment not found")
    if record.status != PaymentStatus.PENDING:
        return HTMLResponse(
            f"<html><body style='font-family:system-ui;background:#0f0f0f;color:#4ade80;"
            f"display:flex;align-items:center;justify-content:center;height:100vh'>"
            f"<h2>Payment {record.status.value}. Check Discord for your result.</h2></body></html>"
        )

    price_atomic = record.price_atomic or DEFAULT_PRICE_ATOMIC
    reqs_dict = {
        "scheme": "exact",
        "network": ARC_NETWORK,
        "maxAmountRequired": str(price_atomic),
        "resource": f"{API_BASE_URL}/execute",
        "description": "NanoPay: premium command",
        "mimeType": "application/json",
        "payTo": record.pay_to or SELLER_WALLET_ADDRESS,
        "maxTimeoutSeconds": 60,
        "asset": ARC_USDC_ADDRESS,
        "extra": {"name": ARC_USDC_NAME, "version": ARC_USDC_VERSION},
    }
    html = build_payment_page(record, reqs_dict, API_BASE_URL)
    return HTMLResponse(html)


@app.post("/execute/{payment_id}")
async def execute_paid_command(payment_id: str, request: Request) -> Response:
    """Gated endpoint. Pay per call with x402, or meter it on the channel."""
    record = await store.get_payment(payment_id)
    if record is None:
        raise HTTPException(404, "Payment not found")

    if record.status == PaymentStatus.PAID:
        return JSONResponse({"ok": True, "result": record.result, "already_paid": True})

    # Look for v2 header first, then v1 fallback
    sig_header = (
        request.headers.get(PAYMENT_SIGNATURE_HEADER)
        or request.headers.get(PAYMENT_SIGNATURE_HEADER.lower())
        or request.headers.get("X-PAYMENT")
        or request.headers.get("x-payment")
    )

    price_atomic = record.price_atomic or DEFAULT_PRICE_ATOMIC
    reqs = _build_requirements(price_atomic, record.pay_to)

    voucher_header = request.headers.get(CHANNEL_VOUCHER_HEADER) or request.headers.get(
        CHANNEL_VOUCHER_HEADER.lower()
    )
    if voucher_header:
        return await _execute_on_channel(record, price_atomic, voucher_header)

    if not sig_header:
        return _402_response(reqs, await _channel_offer(record, price_atomic))

    # Parse payment payload (handles both v1 and v2)
    try:
        payload = decode_payment_signature_header(sig_header)
    except Exception as exc:
        logger.warning("Failed to parse payment header: %s", exc)
        raise HTTPException(400, f"Invalid payment header: {exc}") from exc

    verify_result = await facilitator_client.verify(payload, reqs)
    if not verify_result.is_valid:
        raise HTTPException(402, f"Payment verification failed: {verify_result.invalid_reason}")

    settle_result = await facilitator_client.settle(payload, reqs)
    if not settle_result.success:
        raise HTTPException(402, f"Payment settlement failed: {settle_result.error_reason}")

    tx_hash = settle_result.transaction or ""
    # Normalize to a 0x-prefixed hash so every downstream link (bot receipt, the
    # /demo response, the traction report) resolves on the block explorer, which
    # rejects a bare hash.
    if tx_hash and not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    payer = settle_result.payer or ""

    try:
        result_text = await execute_command(record.command_name, record.command_args)
    except Exception as exc:
        result_text = f"[Command error: {exc}]"

    await store.mark_paid(payment_id, tx_hash, payer, result_text)

    return JSONResponse(
        {
            "ok": True,
            "result": result_text,
            "tx_hash": tx_hash,
            "payer": payer,
        }
    )


async def _execute_on_channel(record: PaymentRecord, price_atomic: int, header: str) -> Response:
    """Meter this call on the channel: check the voucher, then do the work.

    The check is the same one the contract will make, so the service never
    delivers on a voucher it could not redeem, and a refusal here means the same
    thing a revert would mean later.
    """
    if rail is None:
        raise HTTPException(503, "the channel rail is not open on this service")
    try:
        payload = json.loads(base64.b64decode(header))
        voucher, signature = voucher_from_dict(payload)
    except Exception as exc:  # noqa: BLE001 - a bad header is a client error
        raise HTTPException(400, f"invalid channel voucher: {exc}") from exc

    outcome = await rail.record(record.guild_id, record.user_id, price_atomic, voucher, signature)
    if not outcome.accepted:
        await store.mark_failed(record.payment_id, outcome.reason)
        raise HTTPException(402, f"voucher refused: {outcome.reason}")

    try:
        result_text = await execute_command(record.command_name, record.command_args)
    except Exception as exc:  # noqa: BLE001
        result_text = f"[Command error: {exc}]"

    # No per-call transaction, so no hash. The batch that settles this call gets
    # recorded in channel_settlements, which is where the on-chain proof lives.
    await store.mark_paid(record.payment_id, "", rail.payer, result_text)
    task = asyncio.create_task(_settle_when_due())
    _background.add(task)
    task.add_done_callback(_background.discard)

    return JSONResponse(
        {
            "ok": True,
            "rail": "nanochannel",
            "result": result_text,
            "channelId": rail.channel_id_hex,
            "subject": outcome.subject,
            "cumulativeAtomic": outcome.cumulative_atomic,
            "calls": outcome.calls,
            "unsettledAtomic": outcome.unsettled_atomic,
            "capRemainingAtomic": outcome.cap_remaining_atomic,
            "note": "metered off-chain, settles on-chain in a batch",
        }
    )


@app.get("/status/{payment_id}")
async def payment_status(payment_id: str) -> dict[str, Any]:
    record = await store.get_payment(payment_id)
    if record is None:
        raise HTTPException(404, "Payment not found")
    return {
        "payment_id": payment_id,
        "status": record.status.value,
        "tx_hash": record.tx_hash,
        "result": record.result,
        "payer": record.payer_address,
    }


@app.post("/payments/create")
async def create_payment_record(body: dict[str, Any]) -> dict[str, str]:
    record = PaymentRecord(
        guild_id=body.get("guild_id", ""),
        channel_id=body.get("channel_id", ""),
        user_id=body.get("user_id", ""),
        command_name=body.get("command_name", ""),
        command_args=body.get("command_args", {}),
        price_atomic=int(body.get("price_atomic", DEFAULT_PRICE_ATOMIC)),
        pay_to=str(body.get("pay_to", "")),
        interaction_token=body.get("interaction_token", ""),
        application_id=body.get("application_id", ""),
    )
    await store.create_payment(record)
    return {
        "payment_id": record.payment_id,
        "pay_url": f"{API_BASE_URL}/pay/{record.payment_id}",
        "execute_url": f"{API_BASE_URL}/execute/{record.payment_id}",
    }


@app.get("/budget/{user_id}")
async def get_budget(user_id: str) -> dict[str, int]:
    """Per-user spend budget the agent checks before paying."""
    spent = await store.total_spent_atomic(user_id)
    remaining = max(0, DEFAULT_BUDGET_ATOMIC - spent)
    return {
        "limit_atomic": DEFAULT_BUDGET_ATOMIC,
        "spent_atomic": spent,
        "remaining_atomic": remaining,
    }


@app.get("/supported")
async def supported() -> Any:
    return facilitator_client.get_supported()


# ============================================================================
# Marketplace: members list priced services, admins verify, the agent buys
# ============================================================================

# A listing can never price itself above this, so a rogue listing cannot eat a
# whole per-user budget in one call.
MARKET_MAX_PRICE_ATOMIC = 10_000  # $0.01

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@app.post("/market/list")
async def market_list(body: dict[str, Any]) -> dict[str, Any]:
    """A member lists a priced service. It stays invisible to the agent until
    an admin verifies it."""
    name = str(body.get("name", "")).strip()[:40]
    url = str(body.get("url", "")).strip()[:400]
    wallet = str(body.get("wallet", "")).strip()
    guild_id = str(body.get("guild_id", "")).strip()
    lister_id = str(body.get("lister_id", "")).strip()
    description = str(body.get("description", "")).strip()[:200]
    try:
        price_atomic = int(body.get("price_atomic", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "price_atomic must be an integer") from None

    if not (name and url and guild_id and lister_id):
        raise HTTPException(400, "name, url, guild_id and lister_id are required")
    if not re.fullmatch(r"[a-z0-9_]{3,40}", name):
        raise HTTPException(400, "name must be 3-40 chars of a-z, 0-9 and _")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    if not _ADDRESS_RE.fullmatch(wallet):
        raise HTTPException(400, "wallet must be a 0x address (receives the USDC)")
    if not 0 < price_atomic <= MARKET_MAX_PRICE_ATOMIC:
        cap = MARKET_MAX_PRICE_ATOMIC / 1_000_000
        raise HTTPException(400, f"price_atomic must be 1..{MARKET_MAX_PRICE_ATOMIC} (${cap:.2f})")
    if await store.get_service_by_name(guild_id, name) is not None:
        raise HTTPException(409, f"a service named '{name}' already exists in this server")

    service = MarketplaceService(
        guild_id=guild_id,
        lister_id=lister_id,
        name=name,
        description=description,
        url=url,
        price_atomic=price_atomic,
        wallet=wallet,
    )
    await store.create_service(service)
    return {"service_id": service.service_id, "name": service.name, "verified": False}


@app.post("/market/verify")
async def market_verify(body: dict[str, Any]) -> dict[str, Any]:
    """An admin approves a listing; only then can the agent discover and pay it.

    The Discord bot checks the caller's admin permission before calling this.
    """
    guild_id = str(body.get("guild_id", "")).strip()
    name = str(body.get("name", "")).strip()
    admin_id = str(body.get("admin_id", "")).strip()
    if not (guild_id and name and admin_id):
        raise HTTPException(400, "guild_id, name and admin_id are required")
    service = await store.get_service_by_name(guild_id, name)
    if service is None:
        raise HTTPException(404, f"no service named '{name}' in this server")
    if service.verified:
        return {"service_id": service.service_id, "name": service.name, "verified": True}
    await store.verify_service(service.service_id, admin_id)
    return {"service_id": service.service_id, "name": service.name, "verified": True}


@app.get("/market/services/{guild_id}")
async def market_services(guild_id: str, all: bool = False) -> dict[str, Any]:
    """The per-guild catalog. Default shows what the agent can buy (verified);
    ?all=true includes pending listings so admins can see what needs review."""
    services = await store.list_services(guild_id, verified_only=not all)
    return {
        "count": len(services),
        "services": [
            {
                "name": s.name,
                "description": s.description,
                "url": s.url,
                "price_atomic": s.price_atomic,
                "price_usdc": s.price_display,
                "wallet": s.wallet,
                "verified": s.verified,
                "lister_id": s.lister_id,
            }
            for s in services
        ],
    }


@app.get("/settlements")
async def settlements(limit: int = 6) -> dict[str, Any]:
    """Recent real settlements for the public proof wall, newest first.

    The landing page polls this so the "Real USDC, moving on Arc" list reflects
    fresh on-chain activity instead of a fixed set of hashes. Only PAID records
    with a real tx hash are returned, from the same store the traction report reads.
    """
    limit = max(1, min(limit, 25))
    records = await store.recent_settlements(limit)
    rows = [
        {
            # Normalize to 0x so the explorer link resolves. Older demo rows were
            # written before /execute started prefixing the settle hash.
            "tx_hash": r.tx_hash if r.tx_hash.startswith("0x") else "0x" + r.tx_hash,
            "command": r.command_name,
            "amount_atomic": r.price_atomic,
            "amount_usdc": f"{r.price_atomic / 1_000_000:.4f}",
            "result": r.result,
            "source": "web" if r.guild_id == "web" else "discord",
            "paid_at": r.paid_at.isoformat() if r.paid_at else "",
        }
        for r in records
    ]
    return {"count": len(rows), "settlements": rows}


# ============================================================================
# Channel rail: state, quotes and settlement
# ============================================================================


@app.get("/channel")
async def channel_state() -> dict[str, Any]:
    """Everything about the channel in one read: on-chain state, per-person
    meters with their caps, and the batches already settled."""
    if rail is None:
        return {
            "enabled": False,
            "reason": "no open channel for this service, run scripts/open_channel.py",
            "contracts": {
                "nanoChannel": chain_config.NANO_CHANNEL_ADDRESS,
                "spendGuard": chain_config.SPEND_GUARD_ADDRESS,
                "serviceRegistry": chain_config.SERVICE_REGISTRY_ADDRESS,
                "usdc": chain_config.USDC_ADDRESS,
                "explorer": chain_config.ARC_EXPLORER,
                "chainId": chain_config.ARC_CHAIN_ID,
            },
        }
    snapshot = await rail.snapshot()
    snapshot["enabled"] = True
    return snapshot


@app.get("/channel/quote")
async def channel_quote(
    guild_id: str, user_id: str, price_atomic: int = DEFAULT_PRICE_ATOMIC
) -> dict[str, Any]:
    """The cumulative a payer should sign next for this person, and what is left
    of their on-chain cap."""
    if rail is None:
        raise HTTPException(503, "the channel rail is not open on this service")
    quote = await rail.quote(guild_id, user_id, price_atomic)
    return quote.as_dict()


@app.get("/channel/cap")
async def channel_cap_get(guild_id: str, user_id: str) -> dict[str, Any]:
    """One person's on-chain cap and what they have spent against it."""
    if rail is None:
        raise HTTPException(503, "the channel rail is not open on this service")
    return await rail.cap_of(guild_id, user_id)


@app.post("/channel/cap")
async def channel_cap_set(body: dict[str, Any]) -> dict[str, Any]:
    """Set one person's cap in the contract.

    The bot checks the caller is a Discord admin before calling this, the same
    trust boundary the marketplace verify endpoint uses: the API and the bot run
    as one deployment.
    """
    if rail is None:
        raise HTTPException(503, "the channel rail is not open on this service")
    guild_id = str(body.get("guild_id", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    if not (guild_id and user_id):
        raise HTTPException(400, "guild_id and user_id are required")
    try:
        limit_atomic = int(body.get("limit_atomic", 0))
        window_seconds = int(body.get("window_seconds", 86_400))
    except (TypeError, ValueError):
        raise HTTPException(400, "limit_atomic and window_seconds must be integers") from None
    if limit_atomic < 0 or window_seconds < 0:
        raise HTTPException(400, "limit_atomic and window_seconds cannot be negative")

    sent = await rail.set_cap(guild_id, user_id, limit_atomic, window_seconds)
    cap = await rail.cap_of(guild_id, user_id)
    return {
        "ok": sent.ok,
        "txHash": sent.tx_hash,
        "url": chain_config.tx_url(sent.tx_hash),
        "cap": cap,
    }


@app.post("/channel/settle")
async def channel_settle(force: bool = True) -> dict[str, Any]:
    """Redeem the accrued vouchers now. One transaction, however many calls."""
    if rail is None:
        raise HTTPException(503, "the channel rail is not open on this service")
    async with _settle_lock:
        settlement = await rail.settle(force=force)
    if settlement is None:
        pending = await rail.pending_total()
        return {
            "settled": False,
            "pendingAtomic": pending,
            "thresholdAtomic": rail.threshold_atomic,
            "reason": "nothing to collect" if pending == 0 else "under the threshold",
        }
    return {
        "settled": True,
        "txHash": settlement.tx_hash,
        "url": chain_config.tx_url(settlement.tx_hash),
        "totalAtomic": settlement.total_atomic,
        "calls": settlement.calls,
        "subjects": settlement.subject_count,
        "block": settlement.block_number,
        "gasFeeAtomic": settlement.gas_fee_atomic,
    }


@app.post("/demo/ask")
async def demo_ask(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Public browser demo: run the real agent loop and settle on Arc.

    Rate-limited per IP with a global spend cap so public traffic can never drain
    the agent wallet. Returns the real decision, answer, spend and Arc tx hash, so
    the landing page shows a fresh on-chain settlement on every paid ask.
    """
    from ..agent import planner

    ip = request.client.host if request.client else "unknown"
    if not _demo_rate_ok(ip, time.time()):
        raise HTTPException(429, "Slow down a moment, then try again.")

    prompt = str(body.get("prompt", "")).strip()[:240]
    if not prompt:
        return {"action": "free", "answer": "Ask me something.", "spent_atomic": 0}

    # Once the demo hits its global cap, keep answering but stop spending.
    capped = _demo_spent["atomic"] >= DEMO_TOTAL_CAP_ATOMIC
    budget = 0 if capped else DEMO_REQUEST_BUDGET_ATOMIC

    decision = await planner.decide(prompt, budget)

    if decision.action != "pay" or decision.tool is None:
        if decision.action == "decline":
            return {"action": "decline", "reason": decision.reason, "spent_atomic": 0}
        answer = await planner.answer_free(prompt)
        return {"action": "free", "reason": decision.reason, "answer": answer, "spent_atomic": 0}

    tool = decision.tool
    arg_val = decision.args.get(tool.arg_name) or prompt
    record = PaymentRecord(
        guild_id="web",
        channel_id="demo",
        user_id="web-demo",
        command_name=tool.command,
        command_args={tool.arg_name: arg_val},
        price_atomic=tool.price_atomic,
    )
    await store.create_payment(record)

    from ..bot.payer import build_paying_client, pay_and_execute

    payer = build_paying_client()
    try:
        result = await pay_and_execute(payer, record.payment_id)
    finally:
        await payer.aclose()

    _demo_spent["atomic"] += tool.price_atomic
    answer = await planner.compose(prompt, tool.name, result.get("result", ""))
    return {
        "action": "pay",
        "reason": decision.reason,
        "tool": tool.name,
        "answer": answer,
        "spent_atomic": tool.price_atomic,
        "tx_hash": result.get("tx_hash", ""),
    }
