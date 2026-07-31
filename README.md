# MoonWalk

Nanopayment rails for agent commerce on Arc. A USDC payment channel plus
per-person spend caps enforced on-chain, so settling a $0.001 call costs less than
the call.

Built for the Encode x Circle Programmable Money Hackathon, on Arc, Circle's
stablecoin-native L1.

[![live on Arc](https://img.shields.io/badge/live_on-Arc_testnet_5042002-1f1f1f)](https://testnet.arcscan.app/address/0x3e2dE84eD534E39241682957d617ed761892D568)
[![forge tests](https://img.shields.io/badge/forge_tests-51_passing-0f8a56)](#tests-and-verification)
[![pytest](https://img.shields.io/badge/pytest-199_passing-0f8a56)](#tests-and-verification)
[![rails](https://img.shields.io/badge/rails-x402_%2B_payment_channel-2775CA)](#how-it-works)

## The problem

A per-call on-chain transfer costs more than a $0.001 call is worth. Measured on
Arc testnet: one x402 settlement of a $0.001 call burned 87,145 gas and $0.001873
in fees ([`ad1f0d04`](https://testnet.arcscan.app/tx/0xad1f0d044f28535353b9d981293ad12e2f02da583d42374630cce3e6a3057c67)).
The fee is nearly twice the price of the thing being bought. On that rail metered
agent payments either overpay in fees or stop settling per call. The usual
fallback is an operator's database. Then the receipt is a row someone can edit.

The second problem is the wallet. An agent that serves a whole channel spends from
one wallet, so on-chain there is one payer and nothing says whose call was whose.
A per-user budget in the backend is a promise: the operator can change the number
and nobody outside can check it.

## What MoonWalk does

**The channel.** The payer funds once with a signed EIP-3009 authorization, then
signs one EIP-712 voucher per call. A voucher carries a cumulative total for one
subject, so the newest voucher supersedes every earlier one and a lost voucher
costs nothing. The service redeems a batch in a single transaction. Measured on
Arc testnet: 30 metered calls settled in one redeem, 262,639 gas, $0.006565 in
fees. That is 8,755 gas a call against 87,145 on the per-call rail. Per call the
fee drops from $0.001873 to $0.000219.

**The cap.** SpendGuard holds a spend limit per subject in the contract, where a
subject is `keccak256("discord:<guildId>:<userId>")`. Each person in a shared
channel gets their own contract-enforced limit while the agent keeps spending from
one wallet. A voucher that would push a subject past its cap cannot be redeemed by
anyone, the operator included. In the proof run a voucher $0.001 over bob's $0.005
cap was refused with `CapExceeded`.

## Try it

The agent surface is NanoPay, a Discord bot that is live and always on.

- **Join the demo server** and run `/ask` in `#general`: https://discord.gg/JST4tjKWz
- **Add it to your own server**:
  https://discord.com/oauth2/authorize?client_id=1517400111699726488&permissions=18432&scope=bot+applications.commands
- **Landing page**: https://zkasuran.github.io/moonwalk/
- **Source**: https://github.com/zkasuran/moonwalk

The always-on deployment runs this commit, so the channel commands are live:
`/ask <question>` for the agent loop, `/channel` for the channel state and the
per-person meters, `/cap` for an admin to set someone's on-chain limit, `/sell` to
list a priced service on-chain and `/verify-service` for an admin to approve one.
Check the service without Discord: `https://nanopay-api.loadline.xyz/channel`
answers with the live channel state and
`https://nanopay-api.loadline.xyz/market/services/1416577435369214084` answers with
the catalog read from the registry. Everything below also runs against the same live
contracts from a clone, which is the part that does not depend on our uptime.

Reproduce the on-chain proof yourself. The first command is offline, the second
spends real testnet USDC and needs `AGENT_PRIVATE_KEY` plus
`DEPLOYER_PRIVATE_KEY` in `.env`:

```bash
cd contracts && forge test        # 51 tests against the contracts
make channel-demo                 # the full lifecycle on Arc testnet, writes evidence/
```

`make channel-demo` opens a fresh channel, sets caps, signs 30 metered calls,
redeems them in one transaction, has an over-cap voucher refused, closes with both
signatures, then writes every hash to `evidence/channel-<timestamp>.json`. To see
the production channel instead of opening a new one:

```bash
set -a; . ./.env; set +a; .venv/bin/python scripts/open_channel.py
```

## Live on Arc

Chain 5042002, RPC `https://rpc.testnet.arc.network`, explorer
`https://testnet.arcscan.app`. USDC is the system contract
`0x3600000000000000000000000000000000000000`, 6 decimals, EIP-712 domain
`name="USDC"` `version="2"`. All three contracts compiled with solc 0.8.24,
`evm_version = paris`, optimizer on at 200 runs. Full record in
[`deployments/arc-testnet.json`](deployments/arc-testnet.json).

| Contract | Address | Deploy tx |
|---|---|---|
| NanoChannel | [`0x3e2dE84eD534E39241682957d617ed761892D568`](https://testnet.arcscan.app/address/0x3e2dE84eD534E39241682957d617ed761892D568) | [`5a96809f`](https://testnet.arcscan.app/tx/0x5a96809fbd4cd2ab32eaff36d24234916ad2ad643832517e2ac633f8a9ac1833) |
| SpendGuard | [`0xfbB8e1E61e8FbB09e5d5be308ac4F54D2865B67b`](https://testnet.arcscan.app/address/0xfbB8e1E61e8FbB09e5d5be308ac4F54D2865B67b) | [`da0a5e42`](https://testnet.arcscan.app/tx/0xda0a5e42fabc28725fc374cf02825a1eac7e54a77dee2e854fcb73c59010f1b0) |
| ServiceRegistry | [`0x774E5F27b572450F5D21FE3929B45557F3468F9b`](https://testnet.arcscan.app/address/0x774E5F27b572450F5D21FE3929B45557F3468F9b) | [`d201aaeb`](https://testnet.arcscan.app/tx/0xd201aaebfc6c85d909a43cfb08626c481a5dd5249869873dcfc16437c98247d3) |

### The proof run

One pass of `scripts/channel_demo.py` against the live contracts, 2026-07-29.
Every receipt below is status 1 on Arc testnet. Numbers come from
[`evidence/channel-20260729T154745Z.json`](evidence/channel-20260729T154745Z.json).

| Step | Tx | What happened |
|---|---|---|
| open, funded by a signed authorization | [`9923efff`](https://testnet.arcscan.app/tx/0x9923effffcc65964c0998a1ec7abe9a033776af80c6103bb0fe02c5f3fd9ded5) | $0.20 deposit pulled with EIP-3009, 218,926 gas, fee $0.005473 |
| default cap for anyone | [`030f5352`](https://testnet.arcscan.app/tx/0x030f5352856ed5cb57c75445952aa89b6744f94f6705d74423d7b43ca9881673) | $0.005 lifetime, 71,829 gas |
| cap override for alice | [`d0e2bc33`](https://testnet.arcscan.app/tx/0xd0e2bc33a606d8545aefb2ceba5bbcac5c2011857e0198ac02b67a97e792ba28) | $0.06 lifetime, 72,788 gas |
| redeem 30 calls in ONE transaction | [`b779492a`](https://testnet.arcscan.app/tx/0xb779492a6c66abc1d98e4ca13786fd9b968843a9f10e07b0a27620c89f11767a) | $0.03 settled from 2 vouchers, 262,639 gas, fee $0.006565 |
| voucher $0.001 over bob's cap | no transaction | the contract refused it with `CapExceeded`, so it can never be redeemed |
| mutual close | [`622b3619`](https://testnet.arcscan.app/tx/0x622b3619ab023019c422ac1e158bbc14923e8e03a484ee50ba1cb4df9e8e4c4f) | both sides signed, the service submitted, $0.17 refunded, 81,277 gas |

The payer was `0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4`. Its USDC went 9.788000
to 9.758000, exactly the $0.03 that was metered. Its transaction count was **0
before and 0 after**. Thirty paid calls, a deposit, a batch settlement and a close,
and the payer sent nothing: it signed.

The 30 vouchers were signed with a digest built locally, then checked against the
contract's own `voucherHash` view before the batch was submitted
(`0x2f9476d4780d84a615dccede06780e3229725fd3ec1719bcc8faf0720d6fa12e`). A mismatch
between our EIP-712 encoding and the contract's fails there instead of reverting a
settlement later.

### The production channel

Open right now, read with `scripts/open_channel.py`:

| Field | Value |
|---|---|
| channel id | `0xacdc1ad0ca59aa9ff87ca0838e47ce5efd380ae0afd43ad8f6ad8eeb46c7cbdd` |
| payer | `0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4` |
| service | `0xDB6c6340342e71A63cD11Ebac2185204b7777777` |
| deposit / redeemed / outstanding | 0.500000 / 0.000000 / 0.500000 USDC |
| guarded | yes, default cap $0.05 per person per 86,400s |
| cap owner | the service, so the payer never needs to send a transaction |

### The marketplace, on-chain

`/sell` and `/verify-service` write to the ServiceRegistry, so the price the agent
was told and the approval that made a service buyable are public facts. One run
through the live endpoints on 2026-07-31, receipts in
[`evidence/registry-marketplace-2026-07-31.json`](evidence/registry-marketplace-2026-07-31.json):

| Step | Tx | What happened |
|---|---|---|
| a member lists a service | [`51f12944`](https://testnet.arcscan.app/tx/0x51f129449bc8bd8c28a51984a1f2185a5cec8517c63b12bd67e19830e4c41770) | listed at $0.0010 with the member's wallet as `payTo`, invisible to the agent |
| an admin approves it | [`f07f9c2a`](https://testnet.arcscan.app/tx/0xf07f9c2ae748a051801168b181f3d960ab4245d0db2a284691a7cb676d1230c7) | the namespace admin's transaction, and only now is it buyable |

Between the two, `GET /market/services/<guild>` returned an empty catalog for the
agent and showed the listing under `?all=true` for the admin. The first listing in a
server also claims its namespace and pins the $0.01 ceiling on-chain
(`namespaceMaxPrice`), so the contract refuses an over-priced listing even if this
service is wrong about its own rules, and a price change drops the verification.
The operator submits both transactions, because a Discord member has no wallet and
no gas.

## How it works

1. **Fund.** The payer signs an EIP-3009 `ReceiveWithAuthorization` naming the
   channel as `to`. Anyone can submit `open()`; in practice the service does, and
   pays the gas. No approve, no allowance, no transaction from the payer.
2. **Cap.** The cap owner sets a scope default in SpendGuard plus per-subject
   overrides. Unconfigured means zero: a guarded channel with no cap cannot redeem
   anything.
3. **Offer.** `POST /execute/{id}` answers 402 with two offers. The x402 exact
   requirements go in the `PAYMENT-REQUIRED` header, the channel offer in
   `X-CHANNEL-REQUIRED`. The channel offer carries the cumulative to sign and how
   much of that person's cap is left. A client that speaks only x402 ignores the
   second header and pays per call.
4. **Meter.** The payer signs one `Voucher` and resends with `X-CHANNEL-VOUCHER`.
   The service checks the signature, the cumulative and the on-chain cap before it
   does the work, so it never delivers on a voucher it could not redeem. Refusing a
   call and failing to redeem it can never disagree, because both read the same
   contract.
5. **Settle.** Once the accrued total crosses the threshold ($0.02 by default) the
   service calls `redeem()` with the newest voucher per subject. One transaction,
   one USDC transfer, every call since the last redeem. `POST /channel/settle`
   forces it.
6. **Close.** Both sides sign `Close(channelId, redeemed)` and anyone submits
   `closeMutual`, which is how the payer's wallet stays free of transactions for the
   whole life of the channel. If the service stops responding the payer sends
   `requestClose` and then `withdraw` after the challenge window.

### Gasless for whom

Gasless for the payer, not for everybody. The payer signs the deposit, every
voucher and the close. It never sends a transaction. That is measured, not
claimed: transaction count 0 before and 0 after a 30-call run. Someone still pays
gas. Here it is the service, out of the same USDC balance it is collecting into,
because gas on Arc is USDC.

The exception is the unilateral path. `requestClose` and `withdraw` are payer-only,
so a payer that wants its remainder back without the service's cooperation has to
send two transactions and needs USDC to pay for them.

## Circle's own products, wired in

MoonWalk was already running on Circle rails: USDC on Arc, EIP-3009 gasless
authorizations, x402 over HTTP. Two of Circle's own services are now in the code and
exercised live, one is read-only on purpose, and three were rejected with evidence
rather than listed as future work. The whole write-up, including what is UNVERIFIED,
is [`docs/CIRCLE-INTEGRATIONS.md`](docs/CIRCLE-INTEGRATIONS.md).

| Circle product | Status | Code |
|---|---|---|
| Developer-controlled Wallets | working, live on Arc testnet | `src/circle/wallets.py` |
| CCTP V2 with Iris attestation | working, live both directions | `src/circle/cctp.py` |
| Gateway (balances, wallet state) | read only, by choice | `src/circle/gateway.py` |
| Gateway Nanopayments rail | not built, reasoned in the doc | none |
| Faucet API | blocked for our key, HTTP 403 | none |
| Paymaster, StableFX | not on Arc, or permissioned | none |

**A wallet whose key we do not hold.** MoonWalk asks a signer for one thing, an
EIP-712 signature, so `MOONWALK_SIGNER=circle` swaps the local key for a
developer-controlled Circle wallet where this process never sees the key. Circle
signed a channel voucher for chain 5042002 and the digest matched
`NanoChannel.voucherHash()` on Arc byte for byte, then signed an EIP-3009
authorization that a relayer submitted: USDC left the Circle-custodied wallet with
that wallet sending no transaction and paying no gas, transaction count 0 before and
after. Local signing stays the default because a voucher per call cannot afford an
HTTPS round trip, which the doc explains rather than hides.

**The agent refills its own Arc balance.** `scripts/cctp_refill.py` burns USDC on a
source testnet, waits for Circle's Iris attestation and mints on Arc, and the dry run
is the default: it reads both chains, asks Iris for the live fee, builds the calldata
and `eth_call`s it before anything is signed. Three real transfers on 2026-07-29, six
transactions, including a threshold-driven refill the script decided on its own:
Circle charged $0.00005 of the $0.000063 allowed and $0.49995 minted on Arc. Receipts
in `evidence/cctp-live-*.json`.

## New for this hackathon vs what already existed


MoonWalk grew out of NanoPay. NanoPay is a Discord agent that decides whether to
buy a priced tool and pays sub-cent USDC for it over x402 on Arc. It was built for
the Lepton Agents Hackathon (Canteen x Circle) and it is still the agent surface
and the live bot, at [zkasuran/lepton-discord](https://github.com/zkasuran/lepton-discord).
MoonWalk is the payment rails underneath, written for this
hackathon.

Already existed: the Discord agent and its priced tool catalog, the per-call x402
rail with EIP-3009 and the embedded facilitator, the two-wallet split, the
marketplace listing flow, the landing page.

New here: the three Solidity contracts and their 51 tests, the payment channel with
cumulative per-subject vouchers, per-subject spend caps enforced on-chain, the
`ServiceRegistry` with `/sell` and `/verify-service` writing to it, the `src/chain/`
Python package with committed ABIs, the local-versus-contract digest check, the
dual-rail 402, the `GET /channel`, `GET /channel/quote`, `GET /channel/cap`,
`POST /channel/cap` and `POST /channel/settle` endpoints, the `/channel` and `/cap`
Discord commands, the live proof run and the production channel, the Circle
developer-controlled wallet signer and the CCTP V2 self-refill in `src/circle/`.

## Where the code is

| Path | What |
|---|---|
| `contracts/src/NanoChannel.sol` | the channel: open, top up, redeem a batch, close two ways |
| `contracts/src/SpendGuard.sol` | per-subject caps, lifetime or windowed, fails closed |
| `contracts/src/ServiceRegistry.sol` | namespaced priced catalog, verify-before-buyable |
| `src/chain/` | Python clients plus committed ABIs, so the runtime needs no toolchain |
| `src/payments/channel_rail.py` | the service side: quote, check a voucher, redeem the batch |
| `src/bot/channel_payer.py` | the payer side: read the offer, sign, resend |
| `src/api/app.py` | the dual-rail 402 and the `/channel` endpoints |
| `src/circle/` | Circle's own products: the wallet signer, CCTP V2, Gateway reads |
| `docs/ARCHITECTURE.md` | why each of these decisions went the way it did |
| `docs/CIRCLE-INTEGRATIONS.md` | every Circle product tried, with receipts and the UNVERIFIED list |

## Tests and verification

```bash
make dev-install                  # uv venv + deps
cp .env.example .env              # then fill it in
make lint                         # ruff check, ruff format --check, mypy
make test                         # pytest
cd contracts && forge test        # the contracts
```

Run on 2026-07-31, from this working tree:

```
$ make test
199 passed, 2 warnings in 1.51s

$ make lint
uv run ruff check src/ tests/           All checks passed!
uv run ruff format --check src/ tests/  47 files already formatted
uv run mypy src/                        Success: no issues found in 32 source files

$ cd contracts && forge test
Ran 3 test suites: 51 tests passed, 0 failed, 0 skipped (51 total tests)
  NanoChannel.t.sol      30 passed
  ServiceRegistry.t.sol  11 passed
  SpendGuard.t.sol       10 passed
```

The forge suite includes a fuzz test over redeem (512 runs) and named cases for the
parts that are easy to get wrong: signature malleability, a stale voucher, a
voucher for another channel, a mutual-close signature reused after more spend, and
a guarded channel with no cap configured. The Python suite mocks network and model
calls, so it is deterministic and offline, including the 55 tests over the Circle
wallet signer and the CCTP bridge. Everything on-chain in this README was run
separately against live Arc testnet and is linked above.

## Honest limits

- **Testnet only.** Arc has no mainnet. Every address, receipt and balance here is
  Arc testnet. The payer wallet is a throwaway.
- **The challenge window on this deploy is 1 hour.** `challengeWindow()` on the live
  NanoChannel returns 3600. That is the whole window a service has to redeem after
  the payer asks to close, so a service offline for an hour loses whatever it had
  not redeemed. It is set once at deploy and it should be chosen for the deployment,
  not copied from this demo.
- **An unconfigured cap blocks spend, by design.** SpendGuard fails closed. A
  guarded channel with no default cap and no subject cap cannot redeem a single
  voucher. That is the safe direction, but it does mean opening a channel is two
  steps, not one.
- **A member's listing is submitted by the operator.** `/sell` writes to the
  ServiceRegistry, and the transaction comes from the service wallet, because a
  Discord member has no wallet and no gas. `payTo` is the member's own address, so
  the USDC goes to them, and the listing records the operator as the lister. A
  member who wants to be the on-chain lister has to call `register` themselves.
- **The service trusts nothing, but it does have to hold the vouchers.** Every check
  it makes before delivering is the check the contract will make. It still has to
  keep the signatures: lose the voucher store and it loses the right to collect what
  it has not redeemed yet. Cumulative vouchers keep that survivable, since the newest
  voucher per subject is the only one that matters.
- **The payer must send one transaction to close unilaterally.** `requestClose` and
  `withdraw` are payer-only. The gasless story covers the happy path and the
  cooperative close, not a service that goes dark.
- **A voucher lives 24 hours by default** (`MOONWALK_VOUCHER_TTL`). A leaked voucher
  is worth its own cumulative and nothing more, because the contract only ever pays
  the delta over what that subject already settled, but the window is real.
- **The Circle integrations carry their own UNVERIFIED list.** Circle signing is
  proven for EIP-712 and EIP-3009 on Arc, but wallet creation through the API,
  `signTransaction` and contract-execution transactions are not wired, so a
  Circle-only deployment would still need a local key to submit settlements. The
  Base Sepolia CCTP leg is configured and route-checked, never burned.
  [`docs/CIRCLE-INTEGRATIONS.md`](docs/CIRCLE-INTEGRATIONS.md) lists all of it.

## Security

Testnet only. No real funds. `.env` is gitignored, `.env.example` ships
placeholders. The contracts are unaudited. The payer never hands its key to the
service: the service verifies voucher signatures against the payer's address and
holds nothing but the signatures.

## AI disclosure

AI assistance (Claude, Anthropic) was used in developing this project: the
contracts, the Python chain package, the channel rail, the Circle integrations, the
tests and this README. The design, the review and the verification were done by the
author. Verified before submitting: `make lint` (ruff, ruff format, mypy) clean,
`make test` 199 passing, `forge test` 51 passing, plus the on-chain lifecycle run end
to end against live Arc testnet with every transaction linked above.

