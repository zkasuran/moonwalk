# @moonwalk/nanopay

A TypeScript SDK for the MoonWalk USDC payment channel on Arc mainnet. It wraps
the NanoChannel and SpendGuard contracts so you can open a channel, sign
vouchers off-chain, redeem them in batches then close.

The design splits who signs from who pays gas. The payer signs everything (the
deposit authorization, every voucher, the close agreement) and never sends a
transaction. Whoever wants the money on-chain submits. On Arc they pay that gas
in the same USDC they are collecting. Every amount in this SDK is USDC atomic
units, the 6 decimal ERC-20 view.

Built on [viem](https://viem.sh). TypeScript strict. MIT licensed.

## Install

```sh
npm install @moonwalk/nanopay viem
```

`viem` is a peer you install alongside the SDK.

## Quickstart

Read a live channel on Arc mainnet, then build and sign a voucher. Signing costs
the payer nothing: no gas, no transaction.

```ts
import { NanopayClient } from "@moonwalk/nanopay";
import { privateKeyToAccount } from "viem/accounts";

const client = new NanopayClient();                        // Arc mainnet, public RPC
const payer = privateKeyToAccount(process.env.PAYER_KEY as `0x${string}`);
const service = "0x00000000000000000000000000000000000000AA";
const salt = "0x0000000000000000000000000000000000000000000000000000000000000001";
const channelId = client.channelId(payer.address, service, salt);
const subject = client.subjectId("111111111111111111", "222222222222222222");
console.log("outstanding", await client.outstanding(channelId));
console.log("redeemed", await client.subjectRedeemed(channelId, subject));
const voucher = client.voucher(channelId, subject, 250_000n); // 0.25 USDC cumulative
const signature = await client.signVoucher(payer, voucher);   // gasless, off-chain
console.log("voucher signature", signature);
```

A runnable read-only version is in [`examples/read-live.ts`](./examples/read-live.ts).
It talks to the real chain:

```sh
npm run example:read-live
```

## The two-signature open

Opening a channel needs two signatures from the payer, both built by
`openChannel`. The payer signs both then hands them to any submitter, so the
payer's wallet stays free of transactions.

1. An EIP-3009 `ReceiveWithAuthorization` on USDC. This moves the deposit. The
   `to` is fixed to the NanoChannel, so only the channel can pull the funds. It
   is signed under the USDC domain (name "USDC", version "2").
2. The `Open` struct under the NanoChannel domain (name "MoonWalk NanoChannel",
   version "1"). It binds every channel parameter (`guarded`, `capOwner`, the
   opening cap) plus the deposit and the authorization nonce into one signature.
   Without it a submitter could open the channel with `guarded` off or point
   `capOwner` at itself, so this second signature is what makes the caps hold.

```ts
const { channelId, authorization, openSignature, to, data, submit } =
  await client.openChannel({
    payer,
    service,
    salt,
    guarded: true,
    deposit: 5_000_000n,      // 5 USDC
    capLimit: 1_000_000n,     // 1 USDC default cap per subject
    capWindow: 86_400n,       // per day
  });

// Hand { to, data } to any relayer or submit through the client's wallet:
// const hash = await submit();
```

## API

`NanopayClient` is the entry point. Construct it with no arguments for Arc
mainnet over the public RPC. You can also pass a viem `publicClient`, a
`walletClient` for the submitter, address overrides or a chain id.

Writes return `{ to, data, submit }`. Use `to` and `data` with your own relayer,
or call `submit()` to send through the client's wallet client.

- `openChannel(params)`: build and sign both open signatures, return the ids and a ready transaction.
- `signVoucher(payer, voucher)`: sign one cumulative total. Gasless.
- `voucher(channelId, subject, cumulative, params?)`: construct a voucher with a `validBefore`.
- `redeem(channelId, signedVouchers)`: settle a batch in one transfer.
- `closeMutual(channelId, payerSignature, serviceSignature)`: close immediately with both signatures.
- `requestClose(channelId)`: payer starts the challenge window. Sent by the payer.
- `withdraw(channelId)`: payer takes the remainder once the window ends. Sent by the payer.
- `topUp(channelId, authorization)`: add funds with another signed authorization.

Reads:

- `channelOf(channelId)`, `outstanding(channelId)`, `subjectRedeemed(channelId, subject)`, `challengeWindow()`.
- SpendGuard `capOf(channelId, subject)`, `remaining(channelId, subject)`, `usageOf(channelId, subject)`.
- `usdcBalance(address)`.
- On-chain digests for cross-checks: `openHashOnchain`, `closeHashOnchain`, `domainSeparatorOnchain`.

Helpers, pure and usable without a client:

- `channelId(payer, service, salt)`: `keccak256(abi.encode(payer, service, salt))`.
- `subjectId(guildId, userId)`: `keccak256("discord:<guildId>:<userId>")`.
- `voucherDigest`, `openDigest`, `closeDigest`: the EIP-712 digest computed locally.
- `signOpen`, `signClose`, `signReceiveWithAuthorization`, `buildAuthorization`: the standalone signers.

## Amounts and decimals

Every amount here is USDC atomic units, the 6 decimal ERC-20 view. Gas on Arc is
paid in USDC through an 18 decimal native balance. This SDK never touches that 18
decimal view, because mixing the two is the classic Arc decimals bug.

## Addresses (Arc mainnet)

| Contract | Address |
| --- | --- |
| USDC | `0x3600000000000000000000000000000000000000` |
| NanoChannel | `0x059D3A87E91fA91D341f364868A9Ed333077989a` |
| SpendGuard | `0xAbB85ab157357676eBE7ae17A161168912A3c232` |
| ServiceRegistry | `0x1b9FF1FAD0181705B750C325B3A82137bB153866` |

Chain id 5042. RPC `https://rpc.mainnet.arc.io`. Explorer `https://explorer.arc.io`.

The exported `arcMainnet` is a viem chain you can drop into `createPublicClient`
or `createWalletClient`.

## Verifying against the deployed contract

The tests assert the SDK computes the same EIP-712 typehashes the contract stores
on chain (`OPEN_TYPEHASH` and `VOUCHER_TYPEHASH`). They also assert that a voucher
digest the SDK builds matches one computed independently from the raw keccak256
pieces. A match means every signature the SDK produces will verify against the
deployed NanoChannel.

```sh
npm test
```

## License

MIT. See [LICENSE](./LICENSE).

