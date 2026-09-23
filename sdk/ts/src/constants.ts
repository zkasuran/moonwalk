import { defineChain, type Address, type Hex } from "viem";

/**
 * Arc mainnet, where the MoonWalk contracts this SDK wraps are deployed.
 *
 * Gas on Arc is paid in USDC through an 18 decimal native balance. The ERC-20
 * view of USDC is 6 decimals. This SDK only ever works in the 6 decimal atomic
 * unit, the same unit every contract amount uses. The 18 decimal native view
 * appears nowhere here, because mixing the two is the classic Arc decimals bug.
 */
export const ARC_CHAIN_ID = 5042;
export const ARC_RPC_URL = "https://rpc.mainnet.arc.io";
export const ARC_EXPLORER_URL = "https://explorer.arc.io";

export const arcMainnet = defineChain({
  id: ARC_CHAIN_ID,
  name: "Arc",
  nativeCurrency: {
    // Arc settles gas in USDC. The native balance carries 18 decimals even
    // though the ERC-20 view is 6, so a wallet adding this network needs 18 here.
    name: "USD Coin",
    symbol: "USDC",
    decimals: 18,
  },
  rpcUrls: {
    default: { http: [ARC_RPC_URL] },
  },
  blockExplorers: {
    default: { name: "Arc Explorer", url: ARC_EXPLORER_URL },
  },
});

/** USDC uses 6 decimals for its ERC-20 view, and so does every amount here. */
export const USDC_DECIMALS = 6;

/** Live Arc mainnet addresses. Verified against deployments/arc-mainnet.json. */
export const ADDRESSES = {
  usdc: "0x3600000000000000000000000000000000000000",
  nanoChannel: "0x059D3A87E91fA91D341f364868A9Ed333077989a",
  spendGuard: "0xAbB85ab157357676eBE7ae17A161168912A3c232",
  serviceRegistry: "0x1b9FF1FAD0181705B750C325B3A82137bB153866",
} as const satisfies Record<string, Address>;

/**
 * The set of contract addresses a client talks to. Defaults to Arc mainnet.
 * Override any of them to point at another deploy.
 */
export interface MoonwalkAddresses {
  usdc: Address;
  nanoChannel: Address;
  spendGuard: Address;
  serviceRegistry: Address;
}

export const MAINNET_ADDRESSES: MoonwalkAddresses = { ...ADDRESSES };

// ---- EIP-712 domains --------------------------------------------------------

/** USDC EIP-712 domain, for the EIP-3009 and EIP-2612 signatures. */
export const USDC_DOMAIN = {
  name: "USDC",
  version: "2",
} as const;

/** NanoChannel EIP-712 domain, for the Open, Voucher and Close signatures. */
export const NANO_CHANNEL_DOMAIN = {
  name: "MoonWalk NanoChannel",
  version: "1",
} as const;

// ---- EIP-712 type strings and their typehashes ------------------------------
//
// The type strings are the exact ones the contracts hash. The typehash
// constants are the on-chain values the contract stores, quoted here so the
// tests can assert the SDK computes the same keccak256 the contract does.

export const OPEN_TYPE_STRING =
  "Open(address service,bytes32 salt,bool guarded,address capOwner,uint256 deposit,uint256 capLimit,uint64 capWindow,bytes32 authNonce)";

export const VOUCHER_TYPE_STRING =
  "Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)";

export const CLOSE_TYPE_STRING = "Close(bytes32 channelId,uint256 redeemed)";

export const RECEIVE_WITH_AUTHORIZATION_TYPE_STRING =
  "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)";

/** On-chain NanoChannel.OPEN_TYPEHASH. Asserted equal to keccak256(OPEN_TYPE_STRING). */
export const ONCHAIN_OPEN_TYPEHASH: Hex =
  "0x9bd5e1d916549fd486a56398e9a2230b47e1d1f8e6887cbeffc06ced82cf1959";

/** On-chain NanoChannel.VOUCHER_TYPEHASH. Asserted equal to keccak256(VOUCHER_TYPE_STRING). */
export const ONCHAIN_VOUCHER_TYPEHASH: Hex =
  "0x1938bd40683f855f94a3d72f5781fd5efefa46edf4784508f4f202211b7cc140";

// ---- viem typed-data field definitions --------------------------------------
//
// These mirror the struct field order in NanoChannel.sol and USDC. Field order
// is load bearing: a reorder changes the typehash and every signature breaks.

export const OPEN_TYPES = {
  Open: [
    { name: "service", type: "address" },
    { name: "salt", type: "bytes32" },
    { name: "guarded", type: "bool" },
    { name: "capOwner", type: "address" },
    { name: "deposit", type: "uint256" },
    { name: "capLimit", type: "uint256" },
    { name: "capWindow", type: "uint64" },
    { name: "authNonce", type: "bytes32" },
  ],
} as const;

export const VOUCHER_TYPES = {
  Voucher: [
    { name: "channelId", type: "bytes32" },
    { name: "subject", type: "bytes32" },
    { name: "cumulative", type: "uint256" },
    { name: "validBefore", type: "uint64" },
  ],
} as const;

export const CLOSE_TYPES = {
  Close: [
    { name: "channelId", type: "bytes32" },
    { name: "redeemed", type: "uint256" },
  ],
} as const;

export const RECEIVE_WITH_AUTHORIZATION_TYPES = {
  ReceiveWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

/** How long a deposit authorization stays usable if no validBefore is given. */
export const DEFAULT_DEPOSIT_TTL_SECONDS = 3600n;

/** How long a voucher stays redeemable if no validBefore is given. */
export const DEFAULT_VOUCHER_TTL_SECONDS = 86_400n;

export const ZERO_ADDRESS: Address = "0x0000000000000000000000000000000000000000";
