# Security notes and known limitations

MoonWalk holds real USDC on Arc mainnet, so this page states plainly what the
contracts guarantee, what they do not and the bounded limitations an operator or
integrator should design around. Nothing here is hidden in a footnote.

## What the contracts guarantee

- The service is never paid more than the payer signed. Each voucher is a
  cumulative total for one subject and the redeem pays only the delta over what
  that subject already settled.
- The service is never paid more than the deposit. A batch that would exceed the
  channel's outstanding balance reverts.
- A guarded channel enforces every per-subject cap on-chain. A voucher that would
  push a subject past its cap cannot be redeemed by anyone, the operator included.
- The channel parameters are the payer's choice. `open` requires a payer signature
  over the service, salt, guarded flag, cap owner, deposit, opening cap and the
  funding nonce, so a submitter cannot open the payer's channel with different
  policy.
- `settled` is terminal. A closed channel cannot be reopened or redeemed against.
- Signatures are checked for the malleable high-s twin, a bad `v` and the zero
  address before the recovered signer is trusted.

## Liveness assumptions, stated honestly

- The service must redeem a voucher before its `validBefore`. A service offline
  past that loses the unredeemed delta for that subject.
- The payer can exit only after the challenge window and only by sending
  `requestClose` then `withdraw` or by a cooperative `closeMutual` that anyone can
  submit. The zero-transaction property holds on the cooperative path.

## Known limitations, each with its mitigation

- **An over-signed batch reverts whole.** Nothing stops a payer signing vouchers
  summing past the deposit. A cumulative voucher has no partial path, so an
  over-the-deposit batch reverts `Underfunded` and pays nothing. The service checks
  `outstanding(channelId)` before it meters a call, so it never signs itself past
  the deposit.
- **A stale voucher can grief a relayed batch.** `redeem` is permissionless so a
  service can hand a batch to a relayer. A watcher who replays one already-settled
  voucher alone first makes the relayer's batch revert `StaleVoucher` and burn its
  gas. The attacker gains nothing but denial. Submit through a private path or
  retry with the stale voucher dropped.
- **`namespaceMaxPrice` is a write-time guard, not an invariant.** Lowering a
  namespace's max price does not retroactively unverify a listing already above it.
  The admin re-checks listings after lowering the ceiling.
- **The spend window is fixed, not sliding.** A subject can spend up to twice its
  cap across a window boundary (the tail of one window plus the head of the next).
  Set the window to the real budgeting period and size the cap for a single window.
- **The first metered call for a new subject costs more gas than a single call is
  worth.** A guarded cold subject is about 85,000 gas, so at Arc's 20 gwei floor
  that first call costs roughly $0.0017 against a $0.001 price. The channel wins
  once a subject makes several calls between settlements, which is the workload it
  is built for. A one-off caller should use the x402 rail instead.
- **Two Transfer events per USDC movement on Arc.** Arc emits a `Transfer` from the
  USDC contract at 6 decimals and a second from the EIP-7708 system emitter
  `0xfffffffffffffffffffffffffffffffffffffffe` at 18 decimals. An indexer that
  counts both double counts. Filter by emitter address.

## Audit and testing posture

The contracts are covered by 66 Foundry tests including a stateful invariant suite
over token conservation, per-subject accounting and cap enforcement. The two
highest-severity fixes (binding the open to a payer signature and setting the
opening cap atomically so a gasless payer's guarded channel is spendable) were
mutation-tested: reverting each fix makes its test fail, restoring makes it pass.
The contracts are not formally audited by a third party. Deposits are small by
design. Anyone integrating at scale should commission a review first.

## Reporting

Found something? Open an issue on the repository or reach the author at the
contact in the README. There is no bug bounty at this stage.
