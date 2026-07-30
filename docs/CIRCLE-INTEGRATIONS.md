# Circle integrations in MoonWalk

What is wired in, what is deliberately not, and the receipts for both. Everything
below was run against Circle's live testnet APIs and Arc testnet on 2026-07-29
from this repo. Where something could not be established it says UNVERIFIED and no
number is invented.

MoonWalk already ran on Circle rails: USDC on Arc, EIP-3009 gasless
authorizations, x402 for the HTTP half. What was missing was Circle's own SDKs and
services. Two are now in, one is read-only on purpose, and three were rejected
with evidence.

| Circle product | Status | Code |
|---|---|---|
| Developer-controlled Wallets | working, live on Arc testnet | `src/circle/wallets.py`, `scripts/circle_wallet_demo.py` |
| CCTP V2 (+ Iris attestation) | working, live both directions | `src/circle/cctp.py`, `scripts/cctp_refill.py` |
| Gateway (balances, wallet state) | read only, by choice | `src/circle/gateway.py` |
| Gateway Nanopayments rail | not built, reasoned below | none |
| Faucet API (`/v1/faucet/drips`) | blocked for our key, HTTP 403 | none |
| Paymaster | not deployed on Arc, and pointless there | none |
| StableFX | permissioned, no public call shapes | none |

Offline tests: `tests/test_circle_wallets.py` (20) and `tests/test_circle_cctp.py`
(35). They never touch a network. `make test` is 196 passing, `make lint` is clean
(ruff, ruff format, mypy strict on `src/`).

## 1. Circle developer-controlled wallets as the agent's signer

MoonWalk asks a wallet for exactly one thing: an EIP-712 signature. Channel
vouchers, close agreements and EIP-3009 authorizations are all typed data. So the
signer abstraction in `src/circle/wallets.py` is one property and one method, with
two backends behind it:

- `LocalSigner`, the key in this process out of `.env`. Still the default.
- `CircleWalletSigner`, the key inside Circle's infrastructure. This process never
  holds it. Signing is `POST /v1/w3s/developer/sign/typedData`.

That is the honest path to production for an agent that spends money: custody is
Circle's, access is an API credential you can rotate, and a leaked clone of the
repo does not leak the wallet.

### What was proven, live

`.venv/bin/python scripts/circle_wallet_demo.py --fund 50000 --amount 20000 --live`

The wallet Circle holds, read from `GET /v1/w3s/wallets/{id}`:

```
id            62f9550a-59d8-54bc-a044-c7d97c442b78
address       0x5074b92189e295f46597037f3b972786578d05d2
blockchain    ARC-TESTNET
accountType   EOA
custodyType   DEVELOPER
state         LIVE
```

**Arc is supported for signing.** Circle signed a MoonWalk channel voucher for
chain 5042002 and the signature recovered to the wallet address. The payload was
checked against the contract's own hashing rather than against a second copy of our
encoder: `NanoChannel.voucherHash()` on Arc returned
`0x1935a4819784937d889e9219af78d6dcd267724dceab134ebfb0776cee5f1bfb`, byte for byte
what we built locally and handed to Circle.

**Arc USDC accepts the wallet's EIP-3009 signature.** Circle signed a
`TransferWithAuthorization`, and the relayer submitted it. USDC moved out of the
Circle-custodied wallet with the wallet sending no transaction and paying no gas:

| Step | Tx | Result |
|---|---|---|
| relayer funds the wallet, $0.05 | [`0x6007b636`](https://testnet.arcscan.app/tx/0x6007b636612f6bc0b597d57de8382385d6adb63a321683a39db2e34164163eb6) | wallet balance $0 to $0.05 |
| relayer submits Circle's authorization, $0.02 | [`0xfd1742ab`](https://testnet.arcscan.app/tx/0xfd1742ab87070957f616b701985fd2a877b8b812333ef442e2305a64f77f5bcf) | wallet $0.05 to $0.03, payee +$0.02, gas 87547 |

The wallet's transaction count was 0 before and 0 after. Full record:
`evidence/circle-wallet-20260729T164756Z.json`.

### Why local signing is still the default

`MOONWALK_SIGNER=circle` switches the backend. It is not the default, for two
reasons that are about this product rather than about Circle:

1. A metered rail signs a voucher per call. Local signing is microseconds, a Circle
   signature is an HTTPS round trip, and Circle requires the entity secret to be
   re-encrypted for every request. On a nanopayment path that latency is the
   product.
2. Signing is only half of custody. Submitting a settlement still needs a key that
   can send a transaction. Circle's `createDeveloperTransactionContractExecution`
   would cover that, and it is not wired here, so a Circle-only deployment would
   still need a local relayer key. Saying so is more useful than pretending the
   whole rail is custodied.

UNVERIFIED for this integration: wallet creation through the API (we used an
existing wallet), `signTransaction` and the contract-execution transaction API, SCA
or MSCA account types, and anything on Arc mainnet, which does not exist yet.

## 2. CCTP V2: the agent refills its own Arc balance

The agent spends USDC on Arc and pays gas in the same USDC. When the balance falls
under a threshold it has to top itself up, and CCTP V2 is the first-party way to do
it. `src/circle/cctp.py` implements the whole flow in raw calldata so a dry run can
show exactly what would be broadcast:

1. `approve` then `depositForBurn` on the source chain's TokenMessengerV2
2. poll Iris until it signs the burn message
3. `receiveMessage` on the destination MessageTransmitterV2

Before building a burn the bridge asks both chains whether the route is real:
`MessageTransmitterV2.localDomain()` on each side, and
`TokenMinterV2.getLocalToken(sourceDomain, sourceUsdc)` on the destination. A wrong
RPC url or an unsupported pair fails there instead of burning into the void.

```
$ .venv/bin/python scripts/cctp_refill.py --dry-run
route, read from the chains themselves
  Ethereum Sepolia localDomain      0 (expected 0)
  Arc Testnet localDomain  26 (expected 26)
  mints locally as             0x3600000000000000000000000000000000000000
```

The dry run is the default and it is not a mock. It reads both chains, asks Iris for
the live fee, builds the calldata and `eth_call`s it, so a plan is checked by the
contracts before anything is signed:

```
  approve -> 0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238 on eth-sepolia
    calldata 0x095ea7b30000000000000000000000008fe6b999dc680ccfdd5bf7eb0974218be2542daa...
    eth_call ok, this transaction would succeed
  depositForBurn -> 0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA on eth-sepolia
      amount = 251940
      destinationDomain = 26
      mintRecipient = 0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4
      maxFee = 33
      minFinalityThreshold = 1000
    eth_call skipped: needs the approval to land first
```

A revert comes back as the contract's own words rather than as "execution
reverted", and when the destination balance is already above the threshold the plan
builds no calldata at all and says so. Both dry runs are recorded in
`evidence/cctp-dryrun-20260729T1720*.json`.

### What was proven, live

Three real transfers on 2026-07-29, six transactions, no simulation.

**Funding leg, Arc to Ethereum Sepolia, $2.** The Circle faucet is captcha-gated and
its API refuses our key (below), so the source balance was created by bridging out
of Arc, where the wallet already held USDC.

| Call | Chain | Tx | Gas |
|---|---|---|---|
| approve | Arc | [`0xcf361e61`](https://testnet.arcscan.app/tx/0xcf361e61ef08d764d2e4124ef2573646ef218cd83379fc37bd80330b43eef091) | 55438 |
| depositForBurn | Arc | [`0xeff60e4e`](https://testnet.arcscan.app/tx/0xeff60e4eac1a111fb2eabb57a260bca03f3c37d90e532471740cc17c3cb5cfb0) | 120252 |
| receiveMessage | Ethereum Sepolia | [`0x8b4bbc10`](https://sepolia.etherscan.io/tx/0x8b4bbc107965d35ab895aefe13b828ececd5851f231aae23f7b82dd139d550b5) | 171007 |

Attested `complete` on the second poll, five seconds apart. Circle charged 0.
Recipient balance $0 to $2. Record: `evidence/cctp-live-20260729T165032Z.json`.

**Refill leg, Ethereum Sepolia to Arc, $1.** The direction the product actually
needs, with the agent wallet as the mint recipient.

| Call | Chain | Tx | Gas |
|---|---|---|---|
| approve | Ethereum Sepolia | [`0x1a71b14f`](https://sepolia.etherscan.io/tx/0x1a71b14f2e4d928057c8147e5a1ffcbdf55d78f0609b1d57703618b2ccf009db) | 55437 |
| depositForBurn | Ethereum Sepolia | [`0xb130df60`](https://sepolia.etherscan.io/tx/0xb130df607e2522617352f05b1bcb5fa54b7a81403b9816250dd005d4d5228ded) | 109103 |
| receiveMessage | Arc | [`0x9222ec6a`](https://testnet.arcscan.app/tx/0x9222ec6a9d1f1a0eac7f577ef1e8c878bce4a1a648d3bb479bbbfcba411bd3ab) | 175790 |

Agent balance $9.257 to $10.252505. Record:
`evidence/cctp-live-20260729T170159Z.json`.

**Threshold-driven refill, Ethereum Sepolia to Arc, $0.50.** No `--force`: the
balance was under the threshold, so the script decided to bridge on its own.

```
Circle charged   $0.00005 of the $0.000063 allowed
minted           $0.49995 on Arc Testnet
attested message matches the burn: True
recipient balance $10.252505 -> $10.74806 (+$0.495555)
  the $0.004395 gap is the submitter's gas on Arc Testnet
```

Transactions [`0x85632f08`](https://sepolia.etherscan.io/tx/0x85632f089229fd29c756e701d1e5254ed9f2a4d327dd053990d16d23e55aac68),
[`0x1353e27e`](https://sepolia.etherscan.io/tx/0x1353e27eae83eb40e2ec2d9c098ff57cba47036959f47c7162abe5578a3ce453),
[`0xe65307d2`](https://testnet.arcscan.app/tx/0xe65307d2c87e6702f1619abd3ead31df3bd16c06160b8f5bc3d329de593d1d17).
Record: `evidence/cctp-live-20260729T170513Z.json`.

### Three things worth knowing that the docs do not spell out

**The attested message is not the message you emitted.** Comparing Iris's `message`
with the `MessageSent` log byte for byte fails, and not because anything is wrong.
CCTP V2 fills in four fields at attestation time that are zero in the event: the
nonce (12..44), `finalityThresholdExecuted` (144..148), `feeExecuted` (312..344) and
`expirationBlock` (344..376). Measured on both live transfers, nothing else moved.
`attested_message_matches()` allows exactly those ranges, so a message for a
different recipient or amount still fails the check.

**Arc gets attested as finalized even when you ask for Fast.** The Arc burn came
back with `finalityThresholdExecuted` 2000 while the Sepolia burn came back 1000.
Arc finalises in one block, so there is nothing to wait for.

**Fees, read from the message rather than guessed.** Iris quotes basis points and
they are not always whole: `GET /v2/burn/USDC/fees/26/0` is 0 bps Fast, `.../0/26` is
1 bps and `.../6/26` is 1.3, so the fee is computed with a ceiling and a 25 percent
buffer, and `maxFee` never sits under Iris's minimum. On the $0.50 refill Circle
charged 50 atomic units of the 63 allowed. On Arc the mint gas comes out of the same
USDC balance, which is why the recipient's balance moved less than the mint when the
submitter and the recipient were the same wallet.

### What is still UNVERIFIED here

- The Base Sepolia leg. Configured and route-checked, never burned, because the
  wallet holds no Base Sepolia ETH for gas.
- Standard finality (2000) as a request. Both live burns asked for Fast.
- The `--recipient` split where the mint lands on a wallet that did not submit.
- Anything on mainnet or through the mainnet Iris host.

## 3. Gateway Nanopayments next to the MoonWalk channel

Gateway Nanopayments is the closest first-party thing to what MoonWalk built, and it
is live on Arc: domain 26, Nanopayments marked Yes in Circle's supported-blockchains
table, with attestation in about one block (~0.5s), the fastest row in that table.
So this is not a "Circle does not offer it" comparison. It is a choice.

`src/circle/gateway.py` reads Gateway state and nothing else. Live, on Arc testnet:

```
0x5074b92189e295f46597037f3b972786578d05d2
  POST /v1/balances   available 0.001000  pendingBatch 0
  GatewayWallet       availableBalance 1000  totalBalance 1000  (atomic USDC)
  withdrawalDelay()   1209600 seconds = 14 days
  isTokenSupported(USDC) true
```

Circle's API view and the contract's view agree to the unit, which is the thing
worth checking rather than assuming.

### How Gateway does it

A buyer deposits USDC into Circle's `GatewayWallet` contract once, then signs an
EIP-3009 `TransferWithAuthorization` per payment, off chain and gas free. The seller
verifies the signature and serves immediately, credited to a pending balance.
Gateway periodically collects the pending authorizations, verifies each signature and
computes the net balance changes inside an AWS Nitro Enclave, signs the result, and
one on-chain transaction applies it. The contract checks the enclave's signature and
reverts if it is invalid or from an unauthorized signer. Payments go down to
$0.000001 and neither side pays gas per payment.

### What our channel gives that Gateway does not

**Per-subject caps enforced by the contract.** MoonWalk's unit of policy is a Discord
user, not a wallet. `SpendGuard` holds a cap per subject hash and `NanoChannel`
refuses a voucher that would take that subject over it, on chain. Gateway settles
between wallets and publishes no per-recipient or per-subject cap, so the same policy
would have to live in our server, where it is a promise rather than a rule. This is
the actual reason the channel exists.

**No deposit into a third party's contract.** A MoonWalk deposit sits in our channel
contract, pulled in by a `ReceiveWithAuthorization` that only the channel can redeem
because USDC enforces `to == msg.sender`. A Gateway deposit sits in Circle's
contract, and the untouched-funds exit is `initiateWithdrawal` followed by a 14 day
delay, read from the chain above.

**No TEE in the trust model.** Our settlement is `ecrecover` plus a monotonic
cumulative total in Solidity. Anyone can verify a voucher with a public key and
nothing else. Gateway's netting is correct because an enclave says so and the
contract trusts that signer. That is a reasonable design and it is a different one.

**A close both sides sign.** Either party can submit the mutually signed final
figure, and the remainder goes back to the payer in the same transaction.

### Where Gateway is simply better

- **One balance across 13 chains, minted in under 500 ms.** Our channel does not do
  this at all. It is single-chain by construction.
- **Nothing to maintain.** No contract to write or audit, no relayer to run, no
  redeem threshold to tune. Circle carries that engineering.
- **Batching across everyone.** Gateway nets many buyers and sellers into one
  transaction, so gas per payment tends to zero at scale. A channel amortises inside
  itself: one redeem covers N vouchers, but every channel pays its own redeem gas.
- **A seller path that already exists.** Gateway plugs into x402 facilitators and a
  public service marketplace, which is distribution we do not have.
- **A published floor.** $0.000001 per payment as a product guarantee.

One constraint both designs share: Gateway Nanopayments verifies signatures off chain
with `ecrecover`, so smart contract accounts (EIP-1271) cannot buy. Arc USDC's
EIP-3009 path uses `SignatureChecker` and does accept an ERC-1271 signer, so our
channel could take a contract-account payer. Untested here, so UNVERIFIED.

### Why there is no Gateway rail in this repo

A deposit plus burn intent plus mint path would be a second payment rail that does
less than the one already shipped, and Gateway's settlement is Circle's service
rather than something you self-host, so wiring it means replacing our rail, not
extending it. Half a payment rail is worse than none. The read paths are here because
they let this comparison be written from real numbers, and because an operator can
check a Gateway balance without leaving the codebase.

## 4. What we deliberately did not use

**Faucet API, `POST /v1/faucet/drips`.** Blocked for our key. Tried twice with the
real credential, both refused:

```
$ curl -X POST https://api.circle.com/v1/faucet/drips -H "Authorization: Bearer $CIRCLE_API_KEY" \
    -d '{"address":"0x5074...05d2","blockchain":"ARC-TESTNET","usdc":true}'
{"code":3,"message":"Forbidden"}                                       HTTP 403
$ ... '{"address":"0xDB6c...7777","blockchain":"ETH-SEPOLIA","usdc":true}'
{"code":3,"message":"Forbidden"}                                       HTTP 403
```

That matches Circle's own note that the endpoint "requires upgrading to mainnet".
`faucet.circle.com` is reCAPTCHA-gated with no API, so testnet funding is a manual
step. We worked around it by bridging out of Arc, where the wallet already held USDC,
rather than by pretending a drip worked.

**Paymaster.** Both shared testnet addresses have no bytecode on Arc
(`cast code 0x31BE08D3...` and `0x3BA9A96e...` both return `0x`) even though Circle's
docs list ARC-TESTNET, and on Arc gas is already paid in USDC out of the same balance
the ERC-20 view exposes. Sponsorship on Arc is cheaper as EIP-3009 plus a relayer,
which is what MoonWalk already does, with no ERC-4337 stack. Adding the dependency
would have been decoration.

**StableFX.** Permissioned, access via a Circle representative, and
`developers.circle.com/stablefx` publishes no address, ABI or call sequence. The
Arcscan-verified `FxEscrow` shows a relayer records and authorizes trades while makers
and takers deliver, so it is not callable by a random EOA. Nothing was guessed.

## 5. Reproducing this

```bash
# offline, no keys, no network
make test
make lint

# reads Arc, Circle's wallet API and the NanoChannel contract. Signs nothing on chain.
.venv/bin/python scripts/circle_wallet_demo.py

# reads both chains and Iris, builds and eth_calls every transaction, sends none
.venv/bin/python scripts/cctp_refill.py --dry-run

# the live paths
.venv/bin/python scripts/circle_wallet_demo.py --fund 50000 --amount 20000 --live
.venv/bin/python scripts/cctp_refill.py --live --amount 500000
```

Environment, appended to `.env.example`: `CIRCLE_API_KEY`, `CIRCLE_ENTITY_SECRET`,
`CIRCLE_WALLET_ID`, `CIRCLE_WALLET_ADDRESS`, `MOONWALK_SIGNER`,
`CCTP_SOURCE_PRIVATE_KEY`, `CCTP_DEST_PRIVATE_KEY`,
`MOONWALK_REFILL_THRESHOLD_ATOMIC` and the two source-chain RPC overrides. No secret
value appears in any file in this repo, and neither script prints one.

### If the source chain is empty

The refill needs USDC on a source chain and gas there. Both come from outside this
repo, so the one-command path for a human is:

```bash
# 1. get testnet USDC (browser, captcha) and Sepolia ETH (any Sepolia faucet)
#    https://faucet.circle.com  ->  Ethereum Sepolia or Base Sepolia
# 2. then this is the whole refill
.venv/bin/python scripts/cctp_refill.py --source eth-sepolia --live --amount 1000000
```

Or, with USDC already on Arc and no faucet at all, run the bridge outward first and
back afterwards, which is exactly what produced the receipts above:

```bash
.venv/bin/python scripts/cctp_refill.py --source arc-testnet --destination eth-sepolia \
    --live --force --amount 2000000
```

## 6. UNVERIFIED, collected

1. Circle wallet creation, `signTransaction` and the contract-execution transaction
   API. We signed typed data and nothing else through Circle.
2. SCA and MSCA Circle wallets on Arc. Ours is an EOA.
3. The Base Sepolia CCTP leg, for want of Base Sepolia ETH.
4. Standard finality (2000) as a requested threshold.
5. `POST /v1/faucet/drips` with a mainnet-enabled key. Ours is refused with 403.
6. Any Gateway write path: deposit, burn intent, `gatewayMint`. Read only here.
7. An ERC-1271 contract signer against Arc USDC's EIP-3009 path.
8. Everything on Arc mainnet, which is not live.
