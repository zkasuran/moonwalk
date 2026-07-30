"""Refill the agent's USDC across chains with Circle's CCTP V2.

The agent spends USDC on Arc and pays gas in the same USDC, so when the balance
falls under a threshold it has to top itself up. This is that top-up: burn on a
source testnet, wait for Circle's Iris attestation, mint on the destination.

    # read everything, build every call, send nothing
    .venv/bin/python scripts/cctp_refill.py --dry-run

    # the real thing
    .venv/bin/python scripts/cctp_refill.py --live --amount 1000000

    # the other direction, which is how a source balance gets created in the
    # first place on a testnet whose faucet is captcha-gated
    .venv/bin/python scripts/cctp_refill.py --source arc-testnet \
        --destination eth-sepolia --live --amount 2000000 --force

Dry run is the default and it is not a mock: it reads both chains, asks Iris for
the live fee, builds the exact calldata and eth_calls it. Nothing is signed.

Keys come from the environment and are never printed:
  CCTP_SOURCE_PRIVATE_KEY  burns on the source chain (falls back to DEPLOYER_PRIVATE_KEY)
  CCTP_DEST_PRIVATE_KEY    submits the mint on the destination (falls back to
                           AGENT_PRIVATE_KEY, then to the source key)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.signers.local import LocalAccount  # noqa: E402
from web3 import Web3  # noqa: E402

from src.circle import cctp  # noqa: E402

load_dotenv()


def usd(atomic: int) -> str:
    return f"${atomic / 1_000_000:.6f}".rstrip("0").rstrip(".")


def account_from(*env_names: str) -> LocalAccount | None:
    for name in env_names:
        key = os.getenv(name, "")
        if key:
            return Account.from_key(key if key.startswith("0x") else f"0x{key}")
    return None


def native_balance(bridge: cctp.CctpBridge, config: cctp.ChainConfig, address: str) -> int:
    client = bridge.client_for(config.key)
    return int(client.w3.eth.get_balance(Web3.to_checksum_address(address)))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--source", default="eth-sepolia", choices=sorted(cctp.CHAINS))
    ap.add_argument("--destination", default="arc-testnet", choices=sorted(cctp.CHAINS))
    ap.add_argument(
        "--threshold",
        type=int,
        default=int(os.getenv("MOONWALK_REFILL_THRESHOLD_ATOMIC", "10000000")),
        help="refill when the destination balance is under this (atomic USDC)",
    )
    ap.add_argument("--amount", type=int, default=None, help="atomic USDC, default is the deficit")
    ap.add_argument("--recipient", default=None, help="who receives the mint, default the sender")
    ap.add_argument("--sender", default=None, help="only needed when no source key is configured")
    ap.add_argument("--finality", choices=("fast", "standard"), default="fast")
    ap.add_argument(
        "--max-fee", type=int, default=None, help="atomic USDC ceiling for Circle's fee"
    )
    ap.add_argument("--live", action="store_true", help="actually send the transactions")
    ap.add_argument("--dry-run", action="store_true", help="the default, kept for clarity")
    ap.add_argument("--force", action="store_true", help="bridge even if above the threshold")
    ap.add_argument("--iris-timeout", type=float, default=1800.0)
    ap.add_argument("--evidence", default="evidence", help="directory for the run record")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    source = cctp.chain(args.source)
    destination = cctp.chain(args.destination)
    finality = int(
        cctp.FinalityThreshold.FAST if args.finality == "fast" else cctp.FinalityThreshold.STANDARD
    )
    source_account = account_from("CCTP_SOURCE_PRIVATE_KEY", "DEPLOYER_PRIVATE_KEY")
    destination_account = (
        account_from("CCTP_DEST_PRIVATE_KEY", "AGENT_PRIVATE_KEY") or source_account
    )
    sender = args.sender or (source_account.address if source_account else "")
    if not sender:
        print("no source key configured and no --sender given, nothing to plan for")
        return 2
    # The mint lands on whoever holds the destination-side wallet, which for a
    # self-refill is the submitter there. Override with --recipient.
    recipient = args.recipient or (destination_account.address if destination_account else sender)

    bridge = cctp.CctpBridge(source, destination)
    print("CCTP V2 refill")
    print(f"  source        {source.name} (domain {source.domain}, gas in {source.gas_token})")
    print(
        f"  destination   {destination.name} (domain {destination.domain},"
        f" gas in {destination.gas_token})"
    )
    print(f"  burner        {sender}")
    print(f"  mint recipient {recipient}")
    print(f"  mint submitter {destination_account.address if destination_account else '(none)'}")
    print(f"  TokenMessenger {cctp.TOKEN_MESSENGER_V2}")
    print(f"  Transmitter    {cctp.MESSAGE_TRANSMITTER_V2}")
    print(f"  Iris           {bridge.iris.base_url}")

    print("\nroute, read from the chains themselves")
    route = bridge.verify_route()
    print(
        f"  {source.name} localDomain      {route.source_domain_onchain}"
        f" (expected {route.source_domain_expected})"
    )
    print(
        f"  {destination.name} localDomain  {route.destination_domain_onchain}"
        f" (expected {route.destination_domain_expected})"
    )
    print(f"  mints locally as             {route.destination_local_token}")
    if not route.ok:
        print("  route does not check out, refusing to build a burn")
        return 4

    plan = bridge.plan(
        sender=sender,
        recipient=recipient,
        threshold=args.threshold,
        amount=args.amount,
        finality=finality,
        max_fee=args.max_fee,
    )
    if plan.amount == 0:
        allowance_note = "nothing to approve"
    elif plan.needs_approval:
        allowance_note = "too low, approve first"
    else:
        allowance_note = "sufficient"
    print("\nplan")
    print(f"  recipient balance   {usd(plan.destination_balance)} on {destination.name}")
    print(f"  threshold           {usd(plan.threshold_atomic)}")
    print(f"  deficit             {usd(plan.deficit)}")
    print(f"  burner balance      {usd(plan.source_balance)} on {source.name}")
    print(f"  allowance           {usd(plan.allowance)} ({allowance_note})")
    print(f"  bridging            {usd(plan.amount)}")
    print(
        f"  Circle fee ceiling  {usd(plan.max_fee)} at {plan.fee_bps} bps"
        f" (finality {plan.finality})"
    )
    print(f"  arrives at least    {usd(plan.minimum_received)}")
    print(f"  fast burn allowance {bridge.iris.fast_burn_allowance():,.2f} USDC")

    print("\ncalls, built not sent")
    if not plan.calls:
        print("  none: there is nothing to bridge, so no calldata was built")
    for call in plan.calls:
        print(f"  {call.label} -> {call.to} on {call.chain_key}")
        print(f"    {call.function}")
        for name, value in call.args.items():
            print(f"      {name} = {value}")
        print(f"    calldata {call.data}")
        verdict = bridge.simulate(call, plan.sender)
        if call.label == "depositForBurn" and plan.needs_approval:
            print("    eth_call skipped: needs the approval to land first")
        elif verdict is None:
            print("    eth_call ok, this transaction would succeed")
        else:
            print(f"    eth_call reverts: {verdict}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(args.evidence)
    out_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "mode": "live" if args.live else "dry-run",
        "timestamp": stamp,
        "iris": bridge.iris.base_url,
        "route": route.to_dict(),
        "plan": plan.to_dict(),
    }

    gas_note = ""
    if destination_account is not None:
        if destination.gas_token == "USDC":
            have = bridge.destination_reader.usdc_balance(destination_account.address)
            gas_note = f"{usd(have)} USDC on {destination.name}"
        else:
            wei = native_balance(bridge, destination, destination_account.address)
            gas_note = f"{wei / 1e18:.6f} {destination.gas_token} on {destination.name}"
    if source.gas_token != "USDC" and source_account is not None:
        wei = native_balance(bridge, source, source_account.address)
        print(f"\nsource gas: {wei / 1e18:.6f} {source.gas_token} for the burner")
        record["sourceGasWei"] = wei
    if gas_note:
        print(f"mint gas:   {gas_note} for the submitter")

    if not args.live:
        blockers = []
        if not plan.funded:
            blockers.append(
                f"burner holds {usd(plan.source_balance)} on {source.name},"
                f" needs {usd(plan.amount)}"
            )
        if not plan.needed and not args.force:
            blockers.append("destination is already above the threshold, pass --force to bridge")
        if source_account is None:
            blockers.append("no source key: set CCTP_SOURCE_PRIVATE_KEY or DEPLOYER_PRIVATE_KEY")
        record["blockers"] = blockers
        print("\ndry run, nothing was signed and nothing was sent")
        for line in blockers:
            print(f"  blocked: {line}")
        if not blockers:
            print("  ready: rerun with --live")
        path = out_dir / f"cctp-dryrun-{stamp}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        print(f"\nwrote {path}")
        return 0

    if source_account is None or destination_account is None:
        print("\nlive run needs a source key and a destination key")
        return 2
    if not plan.needed and not args.force:
        print("\ndestination is above the threshold, nothing to do (--force overrides)")
        return 0
    if not plan.funded:
        print(
            f"\nburner holds {usd(plan.source_balance)} on {source.name},"
            f" needs {usd(plan.amount)}. Fund it and rerun."
        )
        return 3

    print("\nlive run")
    run = bridge.execute(
        plan,
        source_account=source_account,
        destination_account=destination_account,
        iris_timeout=args.iris_timeout,
        on_step=lambda line: print(f"  {line}"),
    )
    for sent in run.calls:
        print(f"  {sent.label:16} {sent.tx_hash}  gas {sent.gas_used}  {sent.url}")
    print(f"  attestation      {run.attestation_status} (nonce {run.event_nonce})")
    print(f"  Circle charged   {usd(run.fee_charged)} of the {usd(plan.max_fee)} allowed")
    print(f"  minted           {usd(run.minted)} on {destination.name}")
    print(f"  attested message matches the burn: {run.attested_message_consistent}")
    print(
        f"  recipient balance {usd(run.destination_balance_before)}"
        f" -> {usd(run.destination_balance_after)} (+{usd(run.received)})"
    )
    if run.received != run.minted:
        # On Arc the mint gas comes out of the same USDC balance, so when the
        # submitter is the recipient the delta is the mint minus that gas.
        print(
            f"  the {usd(run.minted - run.received)} gap is the submitter's gas on"
            f" {destination.name}"
        )
    record["run"] = run.to_dict()
    path = out_dir / f"cctp-live-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
