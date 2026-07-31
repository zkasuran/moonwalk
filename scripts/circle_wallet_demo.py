"""The agent's wallet, custodied by Circle instead of by this repo.

Same rail, different key custody. MoonWalk normally signs with AGENT_PRIVATE_KEY
out of .env. This demo runs the same EIP-712 payloads through a Circle
developer-controlled wallet, where the key lives in Circle's infrastructure and
this process never holds it.

    # read-only: wallet metadata, a signed voucher, a signature Arc USDC accepts
    .venv/bin/python scripts/circle_wallet_demo.py

    # move real testnet USDC out of the Circle wallet, gaslessly, via EIP-3009
    .venv/bin/python scripts/circle_wallet_demo.py --live --amount 20000

    # top the Circle wallet up first, from the relayer key, if it is empty
    .venv/bin/python scripts/circle_wallet_demo.py --fund 50000 --live

What it proves, in order:
  1. the wallet is real, on ARC-TESTNET, and Circle says it is LIVE
  2. Circle signs a MoonWalk channel voucher and the signature recovers to the
     wallet address, against the digest the NanoChannel contract computes itself
  3. Arc USDC accepts the wallet's EIP-3009 signature, checked by eth_call before
     anything is broadcast
  4. with --live, a relayer submits that authorization and the USDC moves without
     the Circle wallet ever sending a transaction or paying gas

Needs CIRCLE_API_KEY, CIRCLE_ENTITY_SECRET, CIRCLE_WALLET_ID and (optionally)
CIRCLE_WALLET_ADDRESS. None of them are printed.
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
from eth_typing import HexStr  # noqa: E402
from eth_utils.crypto import keccak  # noqa: E402
from web3 import Web3  # noqa: E402
from web3.exceptions import Web3Exception  # noqa: E402

from src.chain import ArcClient, ChannelClient, Voucher, discord_subject  # noqa: E402
from src.chain import config as chain_config  # noqa: E402
from src.circle import cctp, wallets  # noqa: E402

load_dotenv(override=True)  # this lane's .env wins over ambient exports

TRANSFER_WITH_AUTHORIZATION = (
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)"
)
GUILD = "1517400111699726488"

# The same struct NanoChannel.sol hashes, spelled out here so the signature Circle
# produces is checked against the contract's own digest rather than against another
# copy of our own encoder.
VOUCHER_TYPE = [
    {"name": "channelId", "type": "bytes32"},
    {"name": "subject", "type": "bytes32"},
    {"name": "cumulative", "type": "uint256"},
    {"name": "validBefore", "type": "uint64"},
]


def usd(atomic: int) -> str:
    return f"${atomic / 1_000_000:.6f}".rstrip("0").rstrip(".")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def circle_wallet_metadata(wallet_id: str) -> dict[str, Any]:
    """Ask Circle what it holds. Public identifiers only, no key material."""
    from circle.web3 import developer_controlled_wallets as dcw
    from circle.web3 import utils as circle_utils

    client = circle_utils.init_developer_controlled_wallets_client(
        api_key=os.environ["CIRCLE_API_KEY"], entity_secret=os.environ["CIRCLE_ENTITY_SECRET"]
    )
    wallet = dcw.WalletsApi(client).get_wallet(id=wallet_id).data.wallet

    def plain(value: object) -> str:
        # The generated SDK hands back enums, and "ARC-TESTNET" reads better in an
        # evidence file than "Blockchain.ARC_MINUS_TESTNET".
        return str(getattr(value, "value", value))

    return {
        "id": plain(wallet.id),
        "address": plain(wallet.address),
        "blockchain": plain(wallet.blockchain),
        "accountType": plain(wallet.account_type),
        "custodyType": plain(wallet.custody_type),
        "state": plain(wallet.state),
        "walletSetId": plain(wallet.wallet_set_id),
    }


def voucher_typed_data(channel: ChannelClient, voucher: Voucher) -> wallets.TypedData:
    return {
        "types": {"EIP712Domain": wallets.EIP712_DOMAIN, "Voucher": VOUCHER_TYPE},
        "primaryType": "Voucher",
        "domain": {
            "name": chain_config.CHANNEL_DOMAIN_NAME,
            "version": chain_config.CHANNEL_DOMAIN_VERSION,
            "chainId": channel.client.chain_id,
            "verifyingContract": channel.address,
        },
        "message": {
            "channelId": "0x" + voucher.channel_id.hex(),
            "subject": "0x" + voucher.subject.hex(),
            "cumulative": voucher.cumulative,
            "validBefore": voucher.valid_before,
        },
    }


def eip712_digest(typed_data: wallets.TypedData) -> bytes:
    """The 32 bytes an EIP-712 signature actually covers."""
    from eth_account.messages import encode_typed_data

    signable = encode_typed_data(full_message=typed_data)
    return keccak(b"\x19\x01" + signable.header + signable.body)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--amount", type=int, default=20_000, help="atomic USDC to pay ($0.02)")
    ap.add_argument("--fund", type=int, default=0, help="atomic USDC to send the wallet first")
    ap.add_argument(
        "--recipient", default=None, help="who gets paid, default SELLER_WALLET_ADDRESS"
    )
    ap.add_argument("--live", action="store_true", help="broadcast, instead of eth_call only")
    ap.add_argument("--evidence", default="evidence")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    missing = [
        name
        for name in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET", "CIRCLE_WALLET_ID")
        if not os.getenv(name)
    ]
    if missing:
        print(f"missing environment: {', '.join(missing)}")
        return 2
    relayer_key = os.getenv("DEPLOYER_PRIVATE_KEY", "") or os.getenv("FACILITATOR_PRIVATE_KEY", "")
    if not relayer_key:
        print("need DEPLOYER_PRIVATE_KEY or FACILITATOR_PRIVATE_KEY for the relayer")
        return 2

    client = ArcClient()
    client.assert_arc()
    channel = ChannelClient(client)
    relayer = ArcClient.account(relayer_key)
    recipient = Web3.to_checksum_address(
        args.recipient or os.getenv("SELLER_WALLET_ADDRESS") or relayer.address
    )
    evidence: dict[str, Any] = {
        "network": "arc-testnet",
        "chainId": client.chain_id,
        "mode": "live" if args.live else "read-only",
        "steps": [],
    }

    def record(name: str, **fields: Any) -> None:
        evidence["steps"].append({"step": name, **fields})

    print("Circle developer-controlled wallet as the agent signer")
    print(f"  chain        {client.chain_id}")
    print(f"  USDC         {chain_config.USDC_ADDRESS}")
    print(f"  NanoChannel  {channel.address}")
    print(f"  relayer      {relayer.address} (submits and pays gas)")
    print(f"  recipient    {recipient}")

    step(1, "the wallet Circle holds for us")
    meta = circle_wallet_metadata(os.environ["CIRCLE_WALLET_ID"])
    for key, value in meta.items():
        print(f"  {key:13} {value}")
    signer = wallets.CircleWalletSigner.from_env()
    if signer.address.lower() != meta["address"].lower():
        print("  the signer address and the wallet address disagree, stopping")
        return 5
    wallet_balance = client.usdc_balance(signer.address)
    wallet_nonce = client.tx_count(signer.address)
    print(f"  backend       {signer.backend}")
    print(f"  USDC on Arc   {usd(wallet_balance)}")
    print(f"  tx count      {wallet_nonce} (it has never sent a transaction)")
    record(
        "wallet", **meta, backend=signer.backend, usdcAtomic=wallet_balance, txCount=wallet_nonce
    )

    step(2, "Circle signs a MoonWalk channel voucher, the contract's own digest")
    voucher = Voucher(
        channel_id=keccak(text="moonwalk:circle-wallet-demo"),
        subject=discord_subject(GUILD, "circle-wallet-demo"),
        cumulative=args.amount,
        valid_before=int(time.time()) + 3600,
    )
    typed = voucher_typed_data(channel, voucher)
    local_digest = eip712_digest(typed)
    onchain_digest = channel.voucher_hash_onchain(voucher)
    agree = local_digest == onchain_digest
    print(f"  digest, built here      0x{local_digest.hex()}")
    print(f"  digest, from NanoChannel 0x{onchain_digest.hex()}")
    print(f"  agree                   {agree}")
    signature = signer.sign_typed_data(typed)
    recovered = wallets.recover_typed_data_signer(typed, signature)
    matched = recovered.lower() == signer.address.lower()
    print(f"  Circle signature        0x{signature.hex()}")
    print(f"  recovers to             {recovered} (match {matched})")
    record(
        "voucher",
        digestLocal="0x" + local_digest.hex(),
        digestOnchain="0x" + onchain_digest.hex(),
        digestsAgree=agree,
        signature="0x" + signature.hex(),
        recovered=recovered,
        recoveredMatchesWallet=matched,
    )
    if not (agree and matched):
        print("  refusing to go further, the signature does not check out")
        return 5

    step(3, "the same payload through the local signer, for comparison")
    agent_key = os.getenv("AGENT_PRIVATE_KEY", "")
    if agent_key:
        local = wallets.LocalSigner.from_key(agent_key)
        local_signature = local.sign_typed_data(typed)
        local_recovered = wallets.recover_typed_data_signer(typed, local_signature)
        print(f"  backend       {local.backend}")
        print(f"  recovers to   {local_recovered}")
        print("  same interface, same payload, different custody")
        record("local-signer", backend=local.backend, recovered=local_recovered)
    else:
        print("  no AGENT_PRIVATE_KEY set, skipping the comparison")

    if args.fund > 0:
        step(4, f"relayer funds the Circle wallet with {usd(args.fund)}")
        sent = client.send(relayer, client.usdc.functions.transfer(signer.address, args.fund))
        print(f"  {sent.tx_hash}  status {sent.status}  {sent.url}")
        wallet_balance = client.usdc_balance(signer.address)
        print(f"  wallet USDC   {usd(wallet_balance)}")
        record("fund", txHash=sent.tx_hash, amountAtomic=args.fund, url=sent.url)

    step(5, "Circle signs an EIP-3009 authorization, Arc USDC checks it")
    nonce = os.urandom(32)
    valid_before = int(time.time()) + 3600
    authorization = wallets.usdc_authorization_typed_data(
        "TransferWithAuthorization",
        sender=signer.address,
        recipient=recipient,
        value=args.amount,
        valid_after=0,
        valid_before=valid_before,
        nonce=nonce,
    )
    auth_signature = signer.sign_typed_data(authorization)
    auth_recovered = wallets.recover_typed_data_signer(authorization, auth_signature)
    print(f"  authorizer    {signer.address}")
    print(f"  value         {usd(args.amount)} to {recipient}")
    print(f"  nonce         0x{nonce.hex()}")
    print(f"  recovers to   {auth_recovered}")

    call = cctp.BuiltCall(
        label="transferWithAuthorization",
        chain_key="arc-testnet",
        to=Web3.to_checksum_address(chain_config.USDC_ADDRESS),
        data=cctp.encode_call(
            TRANSFER_WITH_AUTHORIZATION,
            Web3.to_checksum_address(signer.address),
            recipient,
            args.amount,
            0,
            valid_before,
            nonce,
            auth_signature,
        ),
        function=TRANSFER_WITH_AUTHORIZATION,
        args={"from": signer.address, "to": recipient, "value": str(args.amount)},
    )
    verdict: str | None
    try:
        client.w3.eth.call(
            {
                "from": Web3.to_checksum_address(relayer.address),
                "to": Web3.to_checksum_address(chain_config.USDC_ADDRESS),
                "data": HexStr(call.data),
            }
        )
        verdict = None
    except Web3Exception as exc:
        verdict = str(exc)
    if verdict is None:
        print("  eth_call ok: USDC accepts the signature and the transfer would land")
    elif "invalid signature" in verdict:
        print(f"  eth_call rejects the signature: {verdict}")
        record("authorization", accepted=False, revert=verdict)
        return 6
    else:
        # FiatToken checks the signature before it checks the balance, so any other
        # revert means the signature itself was accepted.
        print(f"  eth_call reverts after the signature check: {verdict}")
    record(
        "authorization",
        authorizer=signer.address,
        recipient=recipient,
        valueAtomic=args.amount,
        nonce="0x" + nonce.hex(),
        signature="0x" + auth_signature.hex(),
        recovered=auth_recovered,
        ethCall="ok" if verdict is None else verdict,
        calldata=call.data,
    )

    if args.live and verdict is None:
        step(6, "the relayer submits it, the Circle wallet pays no gas")
        before = client.usdc_balance(signer.address)
        recipient_before = client.usdc_balance(recipient)
        sent = cctp.send_call(client, relayer, call, config=cctp.chain("arc-testnet"))
        after = client.usdc_balance(signer.address)
        recipient_after = client.usdc_balance(recipient)
        print(f"  {sent.tx_hash}  status {sent.status}  gas {sent.gas_used}")
        print(f"  {sent.url}")
        print(f"  wallet    {usd(before)} -> {usd(after)}")
        print(f"  recipient {usd(recipient_before)} -> {usd(recipient_after)}")
        print(f"  wallet tx count still {client.tx_count(signer.address)}")
        record(
            "settled",
            txHash=sent.tx_hash,
            url=sent.url,
            gasUsed=sent.gas_used,
            walletBeforeAtomic=before,
            walletAfterAtomic=after,
            recipientBeforeAtomic=recipient_before,
            recipientAfterAtomic=recipient_after,
            walletTxCount=client.tx_count(signer.address),
        )
    elif args.live:
        print("\n  --live asked for a broadcast, but eth_call says it would revert. Not sending.")

    out_dir = Path(args.evidence)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"circle-wallet-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
