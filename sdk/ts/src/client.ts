import {
  bytesToHex,
  createPublicClient,
  encodeFunctionData,
  http,
  type Account,
  type Address,
  type Chain,
  type Hex,
  type PublicClient,
  type Transport,
  type WalletClient,
} from "viem";
import { nanoChannelAbi } from "./abis/nanoChannel.js";
import { spendGuardAbi } from "./abis/spendGuard.js";
import { usdcAbi } from "./abis/usdc.js";
import {
  ARC_CHAIN_ID,
  ARC_RPC_URL,
  arcMainnet,
  DEFAULT_DEPOSIT_TTL_SECONDS,
  DEFAULT_VOUCHER_TTL_SECONDS,
  MAINNET_ADDRESSES,
  ZERO_ADDRESS,
  type MoonwalkAddresses,
} from "./constants.js";
import { closeDigest, openDigest, voucherDigest, type DomainConfig } from "./eip712.js";
import { channelId as deriveChannelId, subjectId as deriveSubjectId } from "./ids.js";
import {
  buildAuthorization,
  signClose,
  signOpen,
  signVoucher,
  type TypedDataSigner,
} from "./signing.js";
import type {
  Authorization,
  Cap,
  ChannelState,
  OpenChannelResult,
  PreparedTransaction,
  SignedVoucher,
  Usage,
  Voucher,
} from "./types.js";

/** A fresh 32 byte EIP-3009 nonce. Random, not a counter, so two never collide. */
export function randomNonce(): Hex {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

function nowSeconds(): bigint {
  return BigInt(Math.floor(Date.now() / 1000));
}
/** Build a read-only viem public client for Arc mainnet. */
export function createArcPublicClient(rpcUrl: string = ARC_RPC_URL): PublicClient {
  return createPublicClient({ chain: arcMainnet, transport: http(rpcUrl) });
}

export interface NanopayClientOptions {
  /** Public client for reads. Defaults to Arc mainnet over the public RPC. */
  publicClient?: PublicClient;
  /** Wallet client for the party that submits transactions and pays gas. Optional. */
  walletClient?: WalletClient<Transport, Chain, Account>;
  /** Address overrides. Defaults to the live Arc mainnet deployment. */
  addresses?: Partial<MoonwalkAddresses>;
  /** Chain id used in the EIP-712 domains. Defaults to Arc mainnet, 5042. */
  chainId?: number;
}

export interface OpenChannelParams {
  /** The payer, who signs both the deposit authorization and the Open struct. */
  payer: TypedDataSigner;
  /** The service that may redeem vouchers on this channel. */
  service: Address;
  /** A 32 byte salt that lets one payer open many channels to one service. */
  salt: Hex;
  /** Whether SpendGuard caps apply to this channel. */
  guarded: boolean;
  /** Deposit in USDC atomic units (6 decimals). */
  deposit: bigint;
  /** Opening default cap for a guarded channel, in USDC atomic units. Default 0. */
  capLimit?: bigint;
  /** Cap window in seconds, 0 for a lifetime total. Default 0. */
  capWindow?: bigint;
  /** Who administers caps later. Default is the zero address, which the contract reads as the payer. */
  capOwner?: Address;
  /** Unix second the deposit authorization becomes valid. Default 0. */
  validAfter?: bigint;
  /** Unix second the deposit authorization expires. Default now + depositTtlSeconds. */
  validBefore?: bigint;
  /** EIP-3009 nonce. Default a fresh random 32 bytes. */
  nonce?: Hex;
  /** TTL applied when validBefore is not given. Default 3600. */
  depositTtlSeconds?: bigint;
}

export interface VoucherParams {
  /** Unix second the voucher expires. Default now + ttlSeconds. */
  validBefore?: bigint;
  /** TTL applied when validBefore is not given. Default 86400. */
  ttlSeconds?: bigint;
}
/**
 * Everything MoonWalk does with the NanoChannel and SpendGuard contracts.
 *
 * Reads and calldata need only a public client. Submitting a transaction needs a
 * wallet client, which any relayer or the service can hold. The payer never
 * needs one: it signs and stops there.
 */
export class NanopayClient {
  readonly publicClient: PublicClient;
  readonly walletClient?: WalletClient<Transport, Chain, Account>;
  readonly addresses: MoonwalkAddresses;
  readonly chainId: number;

  constructor(options: NanopayClientOptions = {}) {
    this.publicClient = options.publicClient ?? createArcPublicClient();
    this.walletClient = options.walletClient;
    this.addresses = { ...MAINNET_ADDRESSES, ...options.addresses };
    this.chainId = options.chainId ?? ARC_CHAIN_ID;
  }

  /** The EIP-712 domain inputs the signing and digest helpers need. */
  get domainConfig(): DomainConfig {
    return {
      chainId: this.chainId,
      nanoChannel: this.addresses.nanoChannel,
      usdc: this.addresses.usdc,
    };
  }

  // ---- id and digest helpers, all pure ---------------------------------

  channelId(payer: Address, service: Address, salt: Hex): Hex {
    return deriveChannelId(payer, service, salt);
  }

  subjectId(guildId: string, userId: string): Hex {
    return deriveSubjectId(guildId, userId);
  }

  voucherDigest(voucher: Voucher): Hex {
    return voucherDigest(this.domainConfig, voucher);
  }

  openDigest(message: Parameters<typeof openDigest>[1]): Hex {
    return openDigest(this.domainConfig, message);
  }

  closeDigest(channelId: Hex, redeemed: bigint): Hex {
    return closeDigest(this.domainConfig, channelId, redeemed);
  }

  /** Construct a voucher with a validBefore, ready to sign. */
  voucher(channelId: Hex, subject: Hex, cumulative: bigint, params: VoucherParams = {}): Voucher {
    const validBefore =
      params.validBefore ?? nowSeconds() + (params.ttlSeconds ?? DEFAULT_VOUCHER_TTL_SECONDS);
    return { channelId, subject, cumulative, validBefore };
  }
  // ---- signing, all by the payer and gasless ---------------------------

  signVoucher(payer: TypedDataSigner, voucher: Voucher): Promise<Hex> {
    return signVoucher(payer, this.domainConfig, voucher);
  }

  signClose(signer: TypedDataSigner, channelId: Hex, redeemed: bigint): Promise<Hex> {
    return signClose(signer, this.domainConfig, channelId, redeemed);
  }

  // ---- reads: NanoChannel ----------------------------------------------

  async channelOf(channelId: Hex): Promise<ChannelState> {
    const ch = await this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "channelOf",
      args: [channelId],
    });
    return {
      payer: ch.payer,
      service: ch.service,
      deposit: ch.deposit,
      redeemed: ch.redeemed,
      closeAt: ch.closeAt,
      guarded: ch.guarded,
      settled: ch.settled,
    };
  }

  outstanding(channelId: Hex): Promise<bigint> {
    return this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "outstanding",
      args: [channelId],
    });
  }

  subjectRedeemed(channelId: Hex, subject: Hex): Promise<bigint> {
    return this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "subjectRedeemed",
      args: [channelId, subject],
    });
  }

  challengeWindow(): Promise<bigint> {
    return this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "challengeWindow",
    });
  }
  // ---- reads: SpendGuard, where the scope is the channel id ------------

  async capOf(channelId: Hex, subject: Hex): Promise<Cap> {
    const [limit, window, set] = await this.publicClient.readContract({
      address: this.addresses.spendGuard,
      abi: spendGuardAbi,
      functionName: "capOf",
      args: [this.addresses.nanoChannel, channelId, subject],
    });
    return { limit, window, set };
  }

  remaining(channelId: Hex, subject: Hex): Promise<bigint> {
    return this.publicClient.readContract({
      address: this.addresses.spendGuard,
      abi: spendGuardAbi,
      functionName: "remaining",
      args: [this.addresses.nanoChannel, channelId, subject],
    });
  }

  async usageOf(channelId: Hex, subject: Hex): Promise<Usage> {
    const [used, windowStart] = await this.publicClient.readContract({
      address: this.addresses.spendGuard,
      abi: spendGuardAbi,
      functionName: "usageOf",
      args: [this.addresses.nanoChannel, channelId, subject],
    });
    return { used, windowStart };
  }

  // ---- reads: USDC ------------------------------------------------------

  usdcBalance(account: Address): Promise<bigint> {
    return this.publicClient.readContract({
      address: this.addresses.usdc,
      abi: usdcAbi,
      functionName: "balanceOf",
      args: [account],
    });
  }

  // ---- reads: on-chain digests, to cross-check the local ones ----------

  closeHashOnchain(channelId: Hex, redeemed: bigint): Promise<Hex> {
    return this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "closeHash",
      args: [channelId, redeemed],
    });
  }

  domainSeparatorOnchain(): Promise<Hex> {
    return this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "domainSeparator",
    });
  }

  openHashOnchain(message: {
    service: Address;
    salt: Hex;
    guarded: boolean;
    capOwner: Address;
    deposit: bigint;
    capLimit: bigint;
    capWindow: bigint;
    authNonce: Hex;
  }): Promise<Hex> {
    return this.publicClient.readContract({
      address: this.addresses.nanoChannel,
      abi: nanoChannelAbi,
      functionName: "openHash",
      args: [
        message.service,
        message.salt,
        message.guarded,
        message.capOwner,
        message.deposit,
        message.capLimit,
        message.capWindow,
        message.authNonce,
      ],
    });
  }

  // ---- writes: build calldata, submit only if a wallet client is set ---

  /**
   * Open and fund a channel. Signs the two things the payer must sign: the
   * EIP-3009 ReceiveWithAuthorization that moves the deposit, and the Open
   * struct that binds every channel parameter and the opening cap. Returns both
   * signatures, the derived channel id and a ready transaction. Submitting it is
   * anyone's job, so hand { to, data } to a relayer or call submit().
   */
  async openChannel(params: OpenChannelParams): Promise<OpenChannelResult> {
    const capLimit = params.capLimit ?? 0n;
    const capWindow = params.capWindow ?? 0n;
    const capOwner = params.capOwner ?? ZERO_ADDRESS;
    const validAfter = params.validAfter ?? 0n;
    const authNonce = params.nonce ?? randomNonce();
    const validBefore =
      params.validBefore ??
      nowSeconds() + (params.depositTtlSeconds ?? DEFAULT_DEPOSIT_TTL_SECONDS);

    const authorization = await buildAuthorization(params.payer, this.domainConfig, {
      value: params.deposit,
      validAfter,
      validBefore,
      nonce: authNonce,
    });
    const openSignature = await signOpen(params.payer, this.domainConfig, {
      service: params.service,
      salt: params.salt,
      guarded: params.guarded,
      capOwner,
      deposit: params.deposit,
      capLimit,
      capWindow,
      authNonce,
    });

    const data = encodeFunctionData({
      abi: nanoChannelAbi,
      functionName: "open",
      args: [
        params.service,
        params.salt,
        params.guarded,
        capOwner,
        capLimit,
        capWindow,
        authorization,
        openSignature,
      ],
    });

    const cid = deriveChannelId(params.payer.address, params.service, params.salt);
    const prepared = this.prepared(this.addresses.nanoChannel, data);
    return { channelId: cid, authorization, openSignature, ...prepared };
  }

  /** Add funds to an open channel with another signed authorization. */
  topUp(channelId: Hex, authorization: Authorization): PreparedTransaction {
    const data = encodeFunctionData({
      abi: nanoChannelAbi,
      functionName: "topUp",
      args: [channelId, authorization],
    });
    return this.prepared(this.addresses.nanoChannel, data);
  }

  /** Settle a batch of vouchers, paying the service in one transfer. */
  redeem(channelId: Hex, signedVouchers: SignedVoucher[]): PreparedTransaction {
    const vouchers = signedVouchers.map((s) => s.voucher);
    const signatures = signedVouchers.map((s) => s.signature);
    const data = encodeFunctionData({
      abi: nanoChannelAbi,
      functionName: "redeem",
      args: [channelId, vouchers, signatures],
    });
    return this.prepared(this.addresses.nanoChannel, data);
  }

  /** Close immediately with both sides' signatures over the same redeemed total. */
  closeMutual(channelId: Hex, payerSignature: Hex, serviceSignature: Hex): PreparedTransaction {
    const data = encodeFunctionData({
      abi: nanoChannelAbi,
      functionName: "closeMutual",
      args: [channelId, payerSignature, serviceSignature],
    });
    return this.prepared(this.addresses.nanoChannel, data);
  }

  /** Payer asks to close. The service can still redeem until the window ends. Sent by the payer. */
  requestClose(channelId: Hex): PreparedTransaction {
    const data = encodeFunctionData({
      abi: nanoChannelAbi,
      functionName: "requestClose",
      args: [channelId],
    });
    return this.prepared(this.addresses.nanoChannel, data);
  }

  /** Payer takes the unspent remainder once the challenge window ends. Sent by the payer. */
  withdraw(channelId: Hex): PreparedTransaction {
    const data = encodeFunctionData({
      abi: nanoChannelAbi,
      functionName: "withdraw",
      args: [channelId],
    });
    return this.prepared(this.addresses.nanoChannel, data);
  }

  // ---- internal ---------------------------------------------------------

  private prepared(to: Address, data: Hex): PreparedTransaction {
    return {
      to,
      data,
      submit: async () => {
        const wallet = this.walletClient;
        if (!wallet) {
          throw new Error(
            "NanopayClient has no wallet client. Use the returned { to, data } with your own relayer, or construct the client with a walletClient.",
          );
        }
        if (!wallet.account) {
          throw new Error("The wallet client has no account set.");
        }
        return wallet.sendTransaction({ account: wallet.account, chain: arcMainnet, to, data });
      },
    };
  }
}

