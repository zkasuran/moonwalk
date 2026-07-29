"""Live proof that the nanopayment channel works on Arc testnet.

Runs the whole lifecycle against the deployed contracts with real USDC:

  1. the agent signs a deposit authorization, the service submits it
  2. the ops wallet sets on-chain per-person caps
  3. thirty metered calls are signed off-chain, one voucher per call
  4. the service redeems the batch, one transaction, one transfer
  5. a voucher over a person's cap is refused by the contract, not by us
  6. both sides sign a close, anyone submits it, the remainder goes back

Nothing here is simulated. Every hash printed is on Arc testnet, and the run
writes an evidence file with the numbers so the README never has to be trusted.

    .venv/bin/python scripts/channel_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_utils import keccak  # noqa: E402
from web3.exceptions import ContractLogicError  # noqa: E402

from src.chain import ArcClient, ChannelClient, GuardClient, Voucher, discord_subject  # noqa: E402
from src.chain import config as chain_config  # noqa: E402
from src.chain.client import revert_name  # noqa: E402

DEPOSIT = 200_000  # $0.20
PRICE = 1_000  # $0.001 per call
DEFAULT_CAP = 5_000  # $0.005 for anyone without their own cap
ALICE_CAP = 60_000  # $0.06
ALICE_CALLS = 25
BOB_CALLS = 5
GUILD = "1517400111699726488"

def usd(atomic: int) -> str:
    return f"${atomic / 1_000_000:.6f}".rstrip("0").rstrip(".")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def main() -> int:
    agent_key = os.getenv("AGENT_PRIVATE_KEY", "")
    ops_key = os.getenv("DEPLOYER_PRIVATE_KEY", "")
    if not agent_key or not ops_key:
        print("need AGENT_PRIVATE_KEY (the payer) and DEPLOYER_PRIVATE_KEY (the service)")
        return 2

    client = ArcClient()
    client.assert_arc()
    channel = ChannelClient(client)
    guard = GuardClient(client)

    payer = ArcClient.account(agent_key)  # signs, never sends a transaction
    service = ArcClient.account(ops_key)  # receives the USDC, submits, pays gas
    evidence: dict[str, Any] = {"network": "arc-testnet", "chainId": client.chain_id, "steps": []}

    def record(name: str, **fields: Any) -> None:
        evidence["steps"].append({"step": name, **fields})

    print("MoonWalk nanopayment channel, live on Arc testnet")
    print(f"  chain            {client.chain_id}")
    print(f"  NanoChannel      {chain_config.NANO_CHANNEL_ADDRESS}")
    print(f"  SpendGuard       {chain_config.SPEND_GUARD_ADDRESS}")
    print(f"  USDC             {chain_config.USDC_ADDRESS}")
    print(f"  payer (agent)    {payer.address}")
    print(f"  service          {service.address}")

    payer_start = client.usdc_balance(payer.address)
    service_start = client.usdc_balance(service.address)
    payer_nonce_start = client.tx_count(payer.address)
    print(f"\n  payer USDC       {usd(payer_start)}")
    print(f"  service USDC     {usd(service_start)}")
    print(f"  payer tx count   {payer_nonce_start}")

    alice = discord_subject(GUILD, "alice-demo")
    bob = discord_subject(GUILD, "bob-demo")
    salt = keccak(text=f"moonwalk-demo:{int(time.time())}")

    step(1, f"payer signs a {usd(DEPOSIT)} deposit, the service submits it")
    auth = channel.sign_deposit(payer, DEPOSIT)
    channel_id, opened = channel.open(
        service, service.address, salt, guarded=True, auth=auth, cap_owner=service.address
    )
    print(f"    channel   0x{channel_id.hex()}")
    print(f"    tx        {opened.url}  (gas {opened.gas_used}, status {opened.status})")
    state = channel.state(channel_id)
    assert state.payer.lower() == payer.address.lower(), "payer mismatch"
    assert state.deposit == DEPOSIT, "deposit mismatch"
    assert state.guarded, "channel should be guarded"
    assert guard.scope_owner(channel_id).lower() == service.address.lower()
    print(f"    on-chain  deposit {usd(state.deposit)}, guarded {state.guarded}")
    record(
        "open",
        channelId="0x" + channel_id.hex(),
        depositAtomic=DEPOSIT,
        txHash=opened.tx_hash,
        block=opened.block_number,
        gasUsed=opened.gas_used,
    )

    step(2, "ops wallet sets the per-person caps on-chain")
    t = guard.set_default_cap(service, channel_id, DEFAULT_CAP, 0)
    print(f"    default {usd(DEFAULT_CAP)} for anyone   {t.url}")
    t2 = guard.set_subject_cap(service, channel_id, alice, ALICE_CAP, 0)
    print(f"    alice   {usd(ALICE_CAP)}                {t2.url}")
    assert guard.remaining(channel_id, alice) == ALICE_CAP
    assert guard.remaining(channel_id, bob) == DEFAULT_CAP
    record("caps", defaultAtomic=DEFAULT_CAP, aliceAtomic=ALICE_CAP, txHashes=[t.tx_hash, t2.tx_hash])

    step(3, f"{ALICE_CALLS + BOB_CALLS} metered calls, one signed voucher each, zero gas")
    signed: dict[str, tuple[Any, bytes]] = {}
    now = int(client.w3.eth.get_block("latest")["timestamp"])
    valid_before = now + chain_config.VOUCHER_TTL_SECONDS
    for name, subject, calls in (("alice", alice, ALICE_CALLS), ("bob", bob, BOB_CALLS)):
        for i in range(1, calls + 1):
            voucher = Voucher(
                channel_id=channel_id,
                subject=subject,
                cumulative=PRICE * i,
                valid_before=valid_before,
            )
            signature = channel.sign_voucher(payer, voucher)
            signed[name] = (voucher, signature)
        print(f"    {name:<6} {calls} calls, cumulative {usd(PRICE * calls)}")

    # Our EIP-712 encoding has to be the contract's, byte for byte. Build the
    # digest locally, then ask the contract what it would hash.
    final_voucher = signed["alice"][0]
    local = channel.voucher_digest_local(final_voucher)
    onchain = channel.voucher_hash_onchain(final_voucher)
    assert local == onchain, f"digest mismatch: local {local.hex()} vs chain {onchain.hex()}"
    print(f"    voucher digest matches the contract: 0x{local.hex()[:24]}...")
    record("vouchers", calls=ALICE_CALLS + BOB_CALLS, pricePerCallAtomic=PRICE,
           digestChecked="0x" + local.hex())

    step(4, "the service redeems the batch: one transaction for every call")
    vouchers = [signed["alice"][0], signed["bob"][0]]
    signatures = [signed["alice"][1], signed["bob"][1]]
    before_service = client.usdc_balance(service.address)
    redeemed = channel.redeem(service, channel_id, vouchers, signatures)
    expected_total = PRICE * (ALICE_CALLS + BOB_CALLS)
    print(f"    tx        {redeemed.url}  (gas {redeemed.gas_used}, status {redeemed.status})")
    print(f"    settled   {usd(expected_total)} in one transfer")
    assert channel.subject_redeemed(channel_id, alice) == PRICE * ALICE_CALLS
    assert channel.subject_redeemed(channel_id, bob) == PRICE * BOB_CALLS

    # The service收 receives the batch and pays the gas out of the same balance,
    # so the net move is the settled total minus the fee in 6 decimal units.
    net = client.usdc_balance(service.address) - before_service
    fee = redeemed.gas_cost_atomic
    assert abs(net - (expected_total - fee)) <= 1, f"net {net}, expected {expected_total - fee}"
    print(f"    service   +{usd(expected_total)} received, -{usd(fee)} gas, net +{usd(net)}")
    print(f"    per-person on-chain: alice {usd(PRICE * ALICE_CALLS)}, bob {usd(PRICE * BOB_CALLS)}")
    print(f"    alice cap left {usd(guard.remaining(channel_id, alice))}")
    record(
        "redeem",
        totalAtomic=expected_total,
        vouchers=len(vouchers),
        callsRepresented=ALICE_CALLS + BOB_CALLS,
        gasFeeAtomic=fee,
        txHash=redeemed.tx_hash,
        block=redeemed.block_number,
        gasUsed=redeemed.gas_used,
    )

    step(5, "a voucher over bob's cap: the contract refuses it")
    over = channel.voucher(channel_id, bob, PRICE * BOB_CALLS + PRICE)
    over_sig = channel.sign_voucher(payer, over)
    refusal = "not refused"
    try:
        client.channel.functions.redeem(channel_id, [over.as_tuple()], [over_sig]).call(
            {"from": service.address}
        )
    except ContractLogicError as exc:
        data = getattr(exc, "data", None)
        refusal = revert_name(data if isinstance(data, str) else None, "SpendGuard", "NanoChannel")
        print(f"    refused with {refusal} (no transaction needed, the call cannot succeed)")
    assert refusal == "CapExceeded", f"expected CapExceeded, got {refusal}"
    record("capRefusal", subject="bob", attemptedAtomic=over.cumulative, error=refusal)

    step(6, "both sides sign the close, the service submits, the remainder returns")
    final_state = channel.state(channel_id)
    payer_close = channel.sign_close(payer, channel_id, final_state.redeemed)
    service_close = channel.sign_close(service, channel_id, final_state.redeemed)
    closed = channel.close_mutual(service, channel_id, payer_close, service_close)
    print(f"    tx        {closed.url}  (gas {closed.gas_used}, status {closed.status})")
    refund = final_state.deposit - final_state.redeemed
    print(f"    refunded  {usd(refund)} back to the payer")
    assert channel.state(channel_id).settled
    record("closeMutual", refundAtomic=refund, txHash=closed.tx_hash, block=closed.block_number)

    payer_end = client.usdc_balance(payer.address)
    service_end = client.usdc_balance(service.address)
    payer_nonce_end = client.tx_count(payer.address)
    print("\nresult")
    print(f"  payer USDC       {usd(payer_start)} -> {usd(payer_end)}")
    print(f"  service USDC     {usd(service_start)} -> {usd(service_end)}")
    print(f"  payer tx count   {payer_nonce_start} -> {payer_nonce_end}")
    print(f"  calls settled    {ALICE_CALLS + BOB_CALLS} for {usd(expected_total)}")
    print("  on-chain txs     3 for the channel (open, redeem, close) plus 2 one-off cap settings")
    assert payer_nonce_end == payer_nonce_start, "the payer must never send a transaction"
    assert payer_start - payer_end == expected_total, "payer paid exactly what was metered"

    evidence["summary"] = {
        "payerStartAtomic": payer_start,
        "payerEndAtomic": payer_end,
        "serviceStartAtomic": service_start,
        "serviceEndAtomic": service_end,
        "payerTxCountStart": payer_nonce_start,
        "payerTxCountEnd": payer_nonce_end,
        "callsSettled": ALICE_CALLS + BOB_CALLS,
        "settledAtomic": expected_total,
        "onchainTransactions": {"channelLifecycle": 3, "oneOffCapSettings": 2},
        "contracts": {
            "nanoChannel": chain_config.NANO_CHANNEL_ADDRESS,
            "spendGuard": chain_config.SPEND_GUARD_ADDRESS,
            "serviceRegistry": chain_config.SERVICE_REGISTRY_ADDRESS,
        },
    }
    out = Path(__file__).resolve().parents[1] / "evidence"
    out.mkdir(exist_ok=True)
    path = out / f"channel-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"\n  evidence         {path.relative_to(Path.cwd()) if str(path).startswith(str(Path.cwd())) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
