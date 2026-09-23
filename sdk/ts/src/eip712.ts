import { hashTypedData, type Address, type Hex, type TypedDataDefinition } from "viem";
import {
  CLOSE_TYPES,
  NANO_CHANNEL_DOMAIN,
  OPEN_TYPES,
  RECEIVE_WITH_AUTHORIZATION_TYPES,
  USDC_DOMAIN,
  VOUCHER_TYPES,
} from "./constants.js";
import type { Authorization, Voucher } from "./types.js";

/**
 * The EIP-712 typed-data builders. One per struct the contracts check.
 *
 * These are the single source of truth for what gets signed. The signing
 * functions pass them to signTypedData, and the digest functions pass the same
 * object to hashTypedData, so a signature and its digest can never drift.
 */

export interface DomainConfig {
  chainId: number;
  nanoChannel: Address;
  usdc: Address;
}

/** Typed data for the EIP-3009 ReceiveWithAuthorization that funds a deposit. */
export function receiveWithAuthorizationTypedData(
  cfg: DomainConfig,
  message: {
    from: Address;
    to: Address;
    value: bigint;
    validAfter: bigint;
    validBefore: bigint;
    nonce: Hex;
  },
): TypedDataDefinition {
  return {
    domain: {
      name: USDC_DOMAIN.name,
      version: USDC_DOMAIN.version,
      chainId: cfg.chainId,
      verifyingContract: cfg.usdc,
    },
    types: RECEIVE_WITH_AUTHORIZATION_TYPES,
    primaryType: "ReceiveWithAuthorization",
    message,
  };
}

/** Typed data for the Open struct the payer signs to open a channel. */
export function openTypedData(
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
): TypedDataDefinition {
  return {
    domain: {
      name: NANO_CHANNEL_DOMAIN.name,
      version: NANO_CHANNEL_DOMAIN.version,
      chainId: cfg.chainId,
      verifyingContract: cfg.nanoChannel,
    },
    types: OPEN_TYPES,
    primaryType: "Open",
    message,
  };
}

/** Typed data for one voucher the payer signs per metered call. */
export function voucherTypedData(cfg: DomainConfig, voucher: Voucher): TypedDataDefinition {
  return {
    domain: {
      name: NANO_CHANNEL_DOMAIN.name,
      version: NANO_CHANNEL_DOMAIN.version,
      chainId: cfg.chainId,
      verifyingContract: cfg.nanoChannel,
    },
    types: VOUCHER_TYPES,
    primaryType: "Voucher",
    message: {
      channelId: voucher.channelId,
      subject: voucher.subject,
      cumulative: voucher.cumulative,
      validBefore: voucher.validBefore,
    },
  };
}

/** Typed data for the Close agreement both sides sign for an immediate close. */
export function closeTypedData(
  cfg: DomainConfig,
  channelId: Hex,
  redeemed: bigint,
): TypedDataDefinition {
  return {
    domain: {
      name: NANO_CHANNEL_DOMAIN.name,
      version: NANO_CHANNEL_DOMAIN.version,
      chainId: cfg.chainId,
      verifyingContract: cfg.nanoChannel,
    },
    types: CLOSE_TYPES,
    primaryType: "Close",
    message: { channelId, redeemed },
  };
}

// ---- local digests ----------------------------------------------------------
//
// The exact 32 byte digest the contract recovers a signer from, computed here
// with no RPC call. Compare against the contract's own view function of the same
// name and a mismatch is a failed assertion rather than a revert in production.

export function voucherDigest(cfg: DomainConfig, voucher: Voucher): Hex {
  return hashTypedData(voucherTypedData(cfg, voucher));
}

export function openDigest(
  cfg: DomainConfig,
  message: Parameters<typeof openTypedData>[1],
): Hex {
  return hashTypedData(openTypedData(cfg, message));
}

export function closeDigest(cfg: DomainConfig, channelId: Hex, redeemed: bigint): Hex {
  return hashTypedData(closeTypedData(cfg, channelId, redeemed));
}

export function receiveWithAuthorizationDigest(
  cfg: DomainConfig,
  message: Parameters<typeof receiveWithAuthorizationTypedData>[1],
): Hex {
  return hashTypedData(receiveWithAuthorizationTypedData(cfg, message));
}

/** The Authorization tuple fields plus a signature, as the message alone. */
export type AuthorizationMessage = Omit<Authorization, "signature">;
