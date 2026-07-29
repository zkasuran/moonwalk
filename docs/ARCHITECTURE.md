# MoonWalk architecture

The README says what this does. This file says why each decision went the way it
did, in the order the decisions bind each other.

The pieces: `NanoChannel` holds the deposit and pays the service. `SpendGuard`
holds the per-person limits and is the only thing that can refuse a redeem.
`ServiceRegistry` is the priced catalog. The payer signs, the service submits, the
contract arbitrates. Nothing lets the service take more than the payer signed for.
Nothing lets the payer deny what it signed.

## Cumulative vouchers, not per-call receipts

A voucher says "this subject has consumed $X in total", not "this call cost
$0.001". That one change is what makes the rest work.

**A lost voucher costs nothing.** If call 17's voucher never reaches the service,
call 18's already covers it, because it carries the higher total. Per-call receipts
would need every single one. A service that dropped one would have to go back to the
payer for a replacement.

**A replayed voucher pays nothing.** `redeem` pays `cumulative - already`, where
`already` is what that subject has settled in this channel. Submitting the same
voucher twice reverts with `StaleVoucher` instead of paying twice, so the service
needs no nonce table.

**A batch stays small.** Settling 30 calls across 2 people takes 2 vouchers, not
30. The proof run did exactly that: 30 calls, 2 vouchers, one transaction, 262,639
gas. With per-call receipts the calldata and the signature recoveries grow with the
call count, which puts the fee back where the channel was meant to remove it.

The cost: the service has to keep the newest voucher per subject. Lose that store
and it loses what it had not redeemed. Taken deliberately, because the alternative
is paying gas per call.

## The subject is a hash

`subject = keccak256("discord:<guildId>:<userId>")`, computed in
`src/chain/subjects.py`.

What it does: gives the contract a stable identity for a person who has no address.
The agent holds one wallet. Without a subject there is one payer on-chain and the
per-person accounting lives only in the operator's database. With it, every redeem
writes `_subjectRedeemed[channelId][subject]` and `SpendGuard` books usage against
the same key, so "how much has this person spent" becomes a chain read.

What it hides: raw Discord ids never enter chain state or an event topic.

What it does not hide: much of anything, against someone who has the ids. A user
snowflake is public and a guild id is public, so anyone holding both recomputes the
subject in one keccak and reads that person's entire spend history. This is not a
privacy claim. It keeps raw platform ids out of permanent chain state while leaving
the accounting auditable by anyone who already knows who they are looking at. Real
unlinkability needs a different construction. This is not it.

The hash is also why the design is not Discord-specific. Anything that produces a
stable string can be a subject. Change the prefix, keep the contracts.

## The cap lives in a contract, not the backend

A backend budget is a promise. The operator can raise the number, edit the row or
forget to check it. Nobody outside can tell.

A cap in `SpendGuard` is a rule. `NanoChannel.redeem` calls `guard.consume` for
every voucher in a guarded channel, so a voucher that would push a subject past its
cap makes the whole redeem revert. The operator cannot redeem it. Neither can a
relayer, neither can the service. In the proof run the over-cap voucher was refused
with `CapExceeded` before any transaction was sent, because the call cannot succeed.

That is what makes the shared-wallet model honest. One agent serves a channel of
people. Each person's limit is enforced by something neither the agent nor the
operator controls at redeem time.

Two details fall out of it.

**Fails closed.** An unconfigured scope and subject pair reverts with
`NotConfigured`. `remaining()` returns 0. A guarded channel with no default cap
cannot spend at all. The safe direction for a spend limit is to block, not to allow.

**Scopes are namespaced by `msg.sender`.** The storage key is
`keccak256(abi.encode(app, scope))` and `consume` always uses `msg.sender` as the
app. So the guard is not tied to NanoChannel. Any payer contract can register scopes
and consume against them. Two apps can never touch each other's usage, even if they
pick the same scope value.

The service also checks the cap off-chain before it does the work
(`channel_rail.record`), but it checks the contract's cap through
`SpendGuard.remaining`, not a local copy. Refusing a call and failing to redeem it
therefore cannot disagree.

## receiveWithAuthorization, not approve plus transferFrom

`approve` plus `transferFrom` needs the payer to send a transaction. That means the
payer needs gas and the whole "the agent never transacts" property is gone at step
one. It also leaves a standing allowance, which is a worse thing to have lying
around than a single-use authorization.

EIP-3009 `receiveWithAuthorization` moves the money on a signature. Arc USDC
implements it at `0x3600000000000000000000000000000000000000` (verified
`name()="USDC"`, `version()="2"`, 6 decimals). The receive variant enforces
`to == msg.sender`. That constraint is the reason to prefer it over
`transferWithAuthorization` here: only the channel contract can redeem the
authorization, so a leaked signature cannot be pointed anywhere else.
`NanoChannel._pull` is the caller, so the signed `to` is the channel address and
nothing else works.

The authorization is single-use. The nonce is a caller-chosen `bytes32` rather than
a counter. The validity window is checked on both ends. `_pull` also compares the
contract's USDC balance before and after and reverts `TransferFailed` unless the
delta is exactly the value, which catches a token that silently no-ops instead of
reverting.

Circle does the same thing on this chain: the verified `GatewayWallet` on Arc
exposes `depositWithAuthorization` and calls `USDC.receiveWithAuthorization` with
itself as `to`.

## The cap owner can be delegated

`open()` takes a `capOwner`. Pass the zero address and it is the payer. In
production MoonWalk passes the service.

The reason is a contradiction in the design. Caps have to be administered by
someone who sends transactions, because `setDefaultCap` and `setSubjectCap` are
transactions. The payer is exactly the party that never sends one. Hardwire cap
ownership to the payer and a gasless payer can never configure a cap, which leaves a
guarded channel permanently unspendable.

Delegating is safe in one direction only. That is the direction it goes. A cap can
only ever restrict what may be redeemed. It cannot authorize a payment. Every
payment still needs the payer's own signature on a voucher for that exact
cumulative. So the worst a hostile cap owner can do is refuse to let the service
collect, which hurts the service rather than the payer. That asymmetry is what makes
the delegation acceptable instead of a hole.

It also puts the control where the humans are. A Discord admin sets a member's cap
with `/cap`, the bot calls the API, the service submits the transaction and the
change lands in the contract instead of a config file.

## What the challenge window protects

`requestClose` starts a timer. `withdraw` only pays out once it expires. On the live
deploy `challengeWindow()` returns 3600.

It protects the service against the payer. Without it a payer could take the deposit
back the instant before the service redeems vouchers it has already delivered work
for. The window is the service's guaranteed time to collect.

It is not protection for the payer. The payer's protection is different and needs no
timer: the contract can only pay the service what the payer signed, and
`Underfunded` caps the total at the deposit. The service cannot invent a voucher.

So the window is a liveness parameter, not a security one. It trades two liveness
risks against each other. Make it short and a service that is offline for the
window loses whatever it had not redeemed. Make it long and a payer whose
service went dark waits that long for its own money. One hour suits a demo. A
production deployment should pick its window from how long its service is allowed to
be down. It is immutable after deploy.

`closeMutual` sidesteps the question. Both sides sign `Close(channelId, redeemed)`
for the current redeemed figure and anyone submits it. Because the digest pins the
exact redeemed amount, a stale close signature becomes worthless the moment the
service redeems more, since the digest changes. And because anyone may submit it,
the payer's wallet stays free of transactions for the entire life of the channel.

## Two rails and when each is right

`POST /execute/{id}` answers 402 with both offers at once. The x402 requirements go
in `PAYMENT-REQUIRED`, the channel offer in `X-CHANNEL-REQUIRED`. The client picks
by which header it sends back: `PAYMENT-SIGNATURE` for x402, `X-CHANNEL-VOUCHER`
for the channel. A client that speaks only x402 ignores the extra header and nothing
changes for it.

**x402 exact is right** for a first call, an anonymous caller or a one-off. There is
no setup: sign one authorization, the facilitator settles, done in a single round
trip. It costs one transaction per call, 87,145 gas measured on Arc, which for a
$0.01 or $0.10 call is noise.

**The channel is right** for a known payer making many small calls. It costs a
deposit up front, an open transaction and a cap configuration. It also needs the
service to keep state. In exchange the per-call cost collapses to 8,755 gas at 30
calls a batch. The payer waits on a signature instead of a block.

The crossover sits where the batch size outgrows the setup cost. At $0.001 a call
the per-call rail spends more on fees than the call is worth, so anything metered
belongs on the channel. `MOONWALK_RAIL` chooses: `auto` tries the channel then falls
back, `channel` refuses any other way, `x402` ignores the channel.

One fallback is deliberately blocked. If the payer has hit its on-chain cap the bot
raises `CapReached` and does not fall back to x402, because paying the same call on
the other rail would walk straight around the limit the cap exists to enforce.

## The registry: list freely, buy only what an admin approved

`ServiceRegistry` is a namespaced priced catalog. A namespace is a hashed community,
`keccak256("discord:<guildId>")`, the same shape as a subject.

Anyone may list. A listing is inert until that namespace's admin calls `setVerified`,
and `isBuyable` is what an agent checks before it pays. The rule that matters is
`setPrice` dropping the verification: an admin approves a service at a price, so a
lister cannot approve at $0.001 then quietly reprice at $0.01. A namespace admin can
also set a ceiling with `setMaxPrice`, which is checked at both register and
reprice time.

The catalog is on-chain for one reason. When the price is a public fact, the price
that was advertised when a payment settled is checkable afterwards by anyone. In a
backend catalog it is a row that was true at the time and nobody can prove it.

`register` also refuses an endpoint that is not `https://`, on the grounds that a
registry other agents read should not advertise a plaintext endpoint.

## The EIP-712 type strings

Verbatim, as hashed by the contracts and by `src/chain/channel.py`.

Domain for both `Voucher` and `Close`:

```
EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
```

with `name = "MoonWalk NanoChannel"`, `version = "1"`, `chainId = 5042002` and
`verifyingContract` set to the NanoChannel address. No salt.

```
Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)
Close(bytes32 channelId,uint256 redeemed)
```

The deposit is signed against USDC's own domain (`name = "USDC"`, `version = "2"`,
`verifyingContract = 0x3600000000000000000000000000000000000000`):

```
ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)
```

`NanoChannel` exposes `VOUCHER_TYPEHASH` and `CLOSE_TYPEHASH` as public constants
and `voucherHash` plus `closeHash` as views, so none of this has to be taken on
trust from a document.

## Two smaller decisions

**The local digest is checked against the contract's.** `channel.py` builds the
EIP-712 digest from scratch and `voucher_hash_onchain` asks the contract what it
would hash. `channel_demo.py` asserts the two are equal before it submits a batch.
A disagreement between two EIP-712 implementations normally surfaces as a settlement
that reverts in production, after the service has already handed over the work. Here
it surfaces as a failed assertion in a demo run. That is why the check sits in the
demo path and not only in a unit test.

**Signature malleability is rejected rather than tolerated.** `_recover` refuses an
`s` above `secp256k1n / 2` and refuses a `v` outside `{27, 28}` before it calls
`ecrecover`, so every signature has exactly one valid encoding. Nothing in the
current design keys off a signature's bytes, so this is precaution rather than a fix
for a known hole. It costs two comparisons.

