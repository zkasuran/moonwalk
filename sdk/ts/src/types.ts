import type { Address, Hex } from "viem";

/**
 * One EIP-3009 authorization the payer signs to fund a channel. The `to` is
 * always the NanoChannel, so only the channel can pull these funds.
 */
export interface Authorization {
  /** The payer, the account whose USDC is authorized. */
  from: Address;
  /** Deposit in USDC atomic units (6 decimals). */
  value: bigint;
  /** Unix second the authorization becomes valid. Usually 0. */
  validAfter: bigint;
  /** Unix second the authorization stops being valid. */
  validBefore: bigint;
  /** Random 32 byte nonce. Not a counter. */
  nonce: Hex;
  /** The payer's EIP-3009 signature over this authorization. */
  signature: Hex;
}

/** One signed statement: this subject has consumed `cumulative` in total. */
export interface Voucher {
  channelId: Hex;
  subject: Hex;
  /** Cumulative spend for the subject in USDC atomic units. */
  cumulative: bigint;
  /** Unix second the voucher stops being redeemable. */
  validBefore: bigint;
}

/** A voucher plus the payer signature that makes it redeemable. */
export interface SignedVoucher {
  voucher: Voucher;
  signature: Hex;
}

/** The on-chain Channel record, as channelOf returns it. */
export interface ChannelState {
  payer: Address;
  service: Address;
  /** Total funded in USDC atomic units. */
  deposit: bigint;
  /** Total already redeemed by the service. */
  redeemed: bigint;
  /** 0 while open, else the unix second the payer may withdraw. */
  closeAt: bigint;
  guarded: boolean;
  settled: boolean;
}

/** A SpendGuard cap that applies to a subject right now. */
export interface Cap {
  /** Allowed spend per window in USDC atomic units. */
  limit: bigint;
  /** Window in seconds. 0 means the limit is a lifetime total. */
  window: bigint;
  /** False when no cap is configured, which means the subject cannot spend. */
  set: boolean;
}

/** SpendGuard usage for a subject in the current window. */
export interface Usage {
  /** Spent in the current window, in USDC atomic units. */
  used: bigint;
  /** Unix second the current window opened. */
  windowStart: bigint;
}

/**
 * A transaction built ready to submit. `to` and `data` are what a relayer needs.
 * `submit` sends it through the client's wallet, when one is configured.
 */
export interface PreparedTransaction {
  /** The contract to call. */
  to: Address;
  /** ABI-encoded calldata. */
  data: Hex;
  /**
   * Submit through the client's wallet client and return the transaction hash.
   * Throws when the client has no wallet client. In that case use `to` and
   * `data` with your own relayer.
   */
  submit: () => Promise<Hex>;
}

/** What openChannel returns: both signatures, the ids and a ready transaction. */
export interface OpenChannelResult extends PreparedTransaction {
  /** The deterministic channel id, derived before the channel exists on chain. */
  channelId: Hex;
  /** The signed EIP-3009 authorization that funds the deposit. */
  authorization: Authorization;
  /** The payer signature over the Open struct. */
  openSignature: Hex;
}
