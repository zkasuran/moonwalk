import type { Address, Hex, LocalAccount, TypedDataDefinition } from "viem";
import {
  closeTypedData,
  openTypedData,
  receiveWithAuthorizationTypedData,
  voucherTypedData,
  type DomainConfig,
} from "./eip712.js";
import type { Authorization, Voucher } from "./types.js";

/**
 * Anything that can sign EIP-712 typed data for the payer. A viem local account
 * from privateKeyToAccount satisfies this, and so does a wallet client's
 * signTypedData. The payer signs everything and sends no transaction.
 */
export interface TypedDataSigner {
  address: Address;
  signTypedData: (typedData: TypedDataDefinition) => Promise<Hex>;
}

/** A viem local account is the common signer. Exported for convenience. */
export type { LocalAccount };

/** Sign the EIP-3009 ReceiveWithAuthorization that funds a deposit. */
export async function signReceiveWithAuthorization(
  signer: TypedDataSigner,
  cfg: DomainConfig,
  message: {
    from: Address;
    to: Address;
    value: bigint;
    validAfter: bigint;
    validBefore: bigint;
    nonce: Hex;
  },
): Promise<Hex> {
  return signer.signTypedData(receiveWithAuthorizationTypedData(cfg, message));
}

/**
 * Build and sign a full Authorization, the deposit half of open. The `to` is
 * fixed to the NanoChannel, so only the channel can pull these funds.
 */
export async function buildAuthorization(
  signer: TypedDataSigner,
  cfg: DomainConfig,
  params: { value: bigint; validAfter: bigint; validBefore: bigint; nonce: Hex },
): Promise<Authorization> {
  const message = {
    from: signer.address,
    to: cfg.nanoChannel,
    value: params.value,
    validAfter: params.validAfter,
    validBefore: params.validBefore,
    nonce: params.nonce,
  };
  const signature = await signReceiveWithAuthorization(signer, cfg, message);
  return {
    from: signer.address,
    value: params.value,
    validAfter: params.validAfter,
    validBefore: params.validBefore,
    nonce: params.nonce,
    signature,
  };
}

/**
 * Sign the Open struct. This is the second payer signature open() needs. It
 * binds the channel parameters and the opening cap to the exact authorization
 * that funds them, so a submitter cannot flip guarded off or repoint capOwner.
 */
export async function signOpen(
  signer: TypedDataSigner,
  cfg: DomainConfig,
  message: {
    service: Address;
    salt: Hex;
    guarded: boolean;
    capOwner: Address;
    deposit: bigint;
    capLimit: bigint;
    capWindow: bigint;
    authNonce: Hex;
  },
): Promise<Hex> {
  return signer.signTypedData(openTypedData(cfg, message));
}

/**
 * Sign one voucher. This is what a metered call costs the payer: a signature,
 * no transaction, no gas, no round trip to a chain.
 */
export async function signVoucher(
  signer: TypedDataSigner,
  cfg: DomainConfig,
  voucher: Voucher,
): Promise<Hex> {
  return signer.signTypedData(voucherTypedData(cfg, voucher));
}

/**
 * Sign the Close agreement. Both sides sign the same Close(channelId, redeemed)
 * digest, so neither can close on a stale number.
 */
export async function signClose(
  signer: TypedDataSigner,
  cfg: DomainConfig,
  channelId: Hex,
  redeemed: bigint,
): Promise<Hex> {
  return signer.signTypedData(closeTypedData(cfg, channelId, redeemed));
}
