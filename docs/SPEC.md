# MoonWalk NanoChannel: the voucher state machine

This is the formal spec for the payment channel: its lifecycle as a state
machine, the EIP-712 structs a payer signs and the safety invariants and
liveness assumptions the contract holds. It is read off `contracts/src/NanoChannel.sol`
and `contracts/src/SpendGuard.sol`. Where the two disagree with this document,
the contract is right and this document is a bug.

## State

A channel is one `Channel` record, keyed by a deterministic id
`channelId = keccak256(abi.encode(payer, service, salt))`. Its fields:

| Field | Meaning |
|---|---|
| `payer` | who funded it and signs every voucher. Zero means the channel does not exist |
| `service` | the only address a redeem can pay |
| `deposit` | total USDC pulled in, across the open and any top ups |
| `redeemed` | total USDC already paid to the service |
| `closeAt` | 0 while open, else the timestamp the payer may withdraw |
| `guarded` | whether SpendGuard caps every redeem |
| `settled` | true once the channel is closed. Terminal |

The persistent states are derived from those fields, not stored as an enum.

| State | Predicate |
|---|---|
| Nonexistent | `payer == 0` |
| Funded (open) | `payer != 0`, `settled == false`, `closeAt == 0` |
| Closing | `payer != 0`, `settled == false`, `closeAt != 0` |
| Settled | `settled == true` |

Two of the task's lifecycle labels are not persistent states here. Open and
Funded coincide: `open()` creates the record and pulls the deposit in one call,
so there is no funded-but-unopened gap. Redeeming is a transition, not a resting
state: a redeem can fire any number of times while the channel is Funded or
Closing and it never changes which state the channel is in.

## Transitions

Each transition lists its precondition (what must hold to call it) and its
postcondition (what is true after it returns). A precondition that fails reverts
with the named error and changes nothing.

### open  (Nonexistent to Funded)

Precondition:
- the derived `channelId` is unused (`payer == 0`), else `ChannelExists`
- `service != 0`, else `ZeroService`
- `auth.value != 0`, else `ZeroDeposit`
- the payer's signature over the `Open` digest recovers to `auth.from`, else `BadSignature`
- the EIP-3009 authorization is valid and moves exactly `auth.value` into the contract, else `TransferFailed`

Postcondition: the channel is Funded with `payer = auth.from`, `service`,
`deposit = auth.value`, `redeemed = 0`, `guarded` as signed. If `guarded`, a
SpendGuard scope is registered for `channelId` owned by `capOwner` (or the payer
when `capOwner == 0`) with the signed opening cap. The deposit sits in the
contract.

The submitter can be anyone. The payer signs two things, the EIP-3009
authorization and the `Open` struct, so the submitter cannot flip `guarded` off
or point `capOwner` at itself. Both signatures are checked in `open()`, which is
what makes SpendGuard's promise hold from the first block.

### topUp  (Funded to Funded)

Precondition: channel is Funded or Closing (`payer != 0`, `!settled`),
`auth.from == payer` else `WrongPayer`, `auth.value != 0` else `ZeroDeposit`.
Postcondition: `deposit += auth.value`, the funds are pulled in.

### redeem  (Funded or Closing, no state change)

Precondition: `payer != 0` and `!settled`, `1 <= vouchers.length == signatures.length <= 256` else `BadBatch` and for every voucher in the batch:
- `channelId` matches, else `WrongChannel`
- `validBefore > block.timestamp`, else `VoucherExpired`
- the signature recovers to `payer`, else `BadSignature`
- `cumulative > alreadyRedeemed[subject]`, else `StaleVoucher`
- if `guarded`, `SpendGuard.consume(channelId, subject, delta)` does not exceed the subject's cap, else `CapExceeded`
- the batch total `<= deposit - redeemed`, else `Underfunded`

Postcondition: for each subject, `alreadyRedeemed[subject] = cumulative`;
`redeemed += total`; `total` USDC is transferred to `service`. A redeem is
all-or-nothing: any failing voucher reverts the whole batch, so nothing is paid.

### requestClose  (Funded to Closing)

Precondition: `payer != 0`, `!settled`, `closeAt == 0` else `AlreadyClosing`,
`msg.sender == payer` else `NotPayer`. Postcondition: `closeAt = now +
challengeWindow`. The service can still redeem until then.

### withdraw  (Closing to Settled)

Precondition: `closeAt != 0` else `NotClosing`, `!settled`, `msg.sender ==
payer` else `NotPayer`, `block.timestamp >= closeAt` else `ChallengeOpen`.
Postcondition: `settled = true`; the unspent remainder `deposit - redeemed` goes
to the payer.

### closeMutual  (Funded or Closing to Settled)

Precondition: `payer != 0`, `!settled` and both the payer's signature and the
service's signature over `Close(channelId, redeemed)` for the current `redeemed`
recover correctly, else `BadSignature`. Postcondition: `settled = true`; the
remainder goes to the payer. Anyone may submit it, so the cooperative close
needs no transaction from the payer.

Once Settled, every one of open, topUp, redeem, requestClose, withdraw and
closeMutual reverts with `ChannelSettled`. Settled is terminal.

## The EIP-712 structs

All three MoonWalk structs are signed under one domain:

```
EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
```

with `name = "MoonWalk NanoChannel"`, `version = "1"`, `chainId` the Arc chain
(5042 on mainnet, 5042002 on testnet) and `verifyingContract` the NanoChannel
address. No salt. The type strings, verbatim as the contract hashes them:

```
Open(address service,bytes32 salt,bool guarded,address capOwner,uint256 deposit,uint256 capLimit,uint64 capWindow,bytes32 authNonce)
Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)
Close(bytes32 channelId,uint256 redeemed)
```

`OPEN_TYPEHASH`, `VOUCHER_TYPEHASH` and `CLOSE_TYPEHASH` are public constants,
and `voucherHash` plus `closeHash` are public views, so an off-chain signer can
assert byte-for-byte agreement with the contract rather than trust two
implementations to match. In the `Open` struct `deposit` is the EIP-3009
`auth.value` and `authNonce` is the EIP-3009 `nonce`, so the two signatures a
payer produces at open are bound to the same amount and the same nonce.

The deposit itself is signed against USDC's own domain, `name = "USDC"`,
`version = "2"`, `verifyingContract = 0x3600000000000000000000000000000000000000`
(the same address on Arc mainnet and testnet):

```
ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)
```

The receive variant enforces `to == msg.sender`, so only the channel contract
can redeem the authorization. A leaked signature cannot be pointed anywhere
else.

## Safety invariants

These hold for any sequence of calls, because the contract enforces them on
every path.

1. **The service is never paid more than the payer signed.** A redeem pays
   `cumulative - alreadyRedeemed` per subject and the cumulative comes from a
   voucher whose signature must recover to the payer. No payer signature, no
   payment. The service cannot invent a voucher.
2. **The service is never paid more than the deposit.** Each redeem checks
   `total <= deposit - redeemed` and reverts `Underfunded` otherwise, so the sum
   of all payouts never exceeds the funds actually pulled in.
3. **No subject exceeds its cap.** In a guarded channel every voucher's delta is
   booked through `SpendGuard.consume`, which reverts `CapExceeded` if the
   subject's usage would pass its cap. A batch that would overspend any subject
   reverts whole, so the cap cannot be crossed by anyone, the operator included.
4. **Settled is terminal.** After `settled == true` every state-changing entry
   point reverts. A channel cannot be reopened, redeemed against or closed twice.
5. **A stale or replayed voucher pays nothing.** Because `cumulative` must be
   strictly greater than what the subject already settled, resubmitting a voucher
   reverts `StaleVoucher` instead of paying twice, so the service needs no nonce
   table.
6. **Every signature has one valid encoding.** `_recover` rejects an `s` above
   `secp256k1n / 2` and a `v` outside `{27, 28}`, so the malleable twin of a
   signature is refused.

## Liveness assumptions

These are not enforced by the contract. They are the timing the two parties must
respect, stated plainly.

- **The service must redeem before `validBefore`.** A voucher is only redeemable
  while `validBefore > now`. An expired voucher is dead weight, so a service that
  sits on vouchers past their window loses the right to collect them. On the
  closing path there is a second deadline: the service must redeem before
  `closeAt`.
- **The payer can exit unilaterally only after the challenge window.** `withdraw`
  pays out only once `block.timestamp >= closeAt`, where `closeAt` was set by
  `requestClose` to `now + challengeWindow`. The window is immutable after deploy
  and at least one hour (`MIN_CHALLENGE_WINDOW`). The cooperative path,
  `closeMutual`, sidesteps the wait: with both signatures the channel settles in
  the next block, which on Arc is final.

