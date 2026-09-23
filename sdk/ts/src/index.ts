/**
 * @moonwalk/nanopay
 *
 * A TypeScript SDK for the MoonWalk USDC payment channel on Arc mainnet. Sign
 * off-chain, settle in batches. The payer signs everything and sends no
 * transaction. Whoever wants the money on-chain submits, and on Arc they pay
 * that gas in the same USDC they are collecting.
 */

export {
  NanopayClient,
  createArcPublicClient,
  randomNonce,
  type NanopayClientOptions,
  type OpenChannelParams,
  type VoucherParams,
} from "./client.js";

export {
  buildAuthorization,
  signClose,
  signOpen,
  signReceiveWithAuthorization,
  signVoucher,
  type LocalAccount,
  type TypedDataSigner,
} from "./signing.js";

export {
  closeDigest,
  closeTypedData,
  openDigest,
  openTypedData,
  receiveWithAuthorizationDigest,
  receiveWithAuthorizationTypedData,
  voucherDigest,
  voucherTypedData,
  type DomainConfig,
} from "./eip712.js";

export { channelId, subjectId, subjectLabel } from "./ids.js";

export type {
  Authorization,
  Cap,
  ChannelState,
  OpenChannelResult,
  PreparedTransaction,
  SignedVoucher,
  Usage,
  Voucher,
} from "./types.js";

export {
  ADDRESSES,
  ARC_CHAIN_ID,
  ARC_EXPLORER_URL,
  ARC_RPC_URL,
  arcMainnet,
  CLOSE_TYPE_STRING,
  CLOSE_TYPES,
  DEFAULT_DEPOSIT_TTL_SECONDS,
  DEFAULT_VOUCHER_TTL_SECONDS,
  MAINNET_ADDRESSES,
  NANO_CHANNEL_DOMAIN,
  ONCHAIN_OPEN_TYPEHASH,
  ONCHAIN_VOUCHER_TYPEHASH,
  OPEN_TYPE_STRING,
  OPEN_TYPES,
  RECEIVE_WITH_AUTHORIZATION_TYPE_STRING,
  RECEIVE_WITH_AUTHORIZATION_TYPES,
  USDC_DECIMALS,
  USDC_DOMAIN,
  VOUCHER_TYPE_STRING,
  VOUCHER_TYPES,
  ZERO_ADDRESS,
  type MoonwalkAddresses,
} from "./constants.js";

export {
  nanoChannelAbi,
  serviceRegistryAbi,
  spendGuardAbi,
  usdcAbi,
} from "./abis/index.js";
