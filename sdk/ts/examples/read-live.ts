/**
 * Read the live MoonWalk NanoChannel on Arc mainnet.
 *
 * This talks to the real chain. It confirms the RPC is Arc mainnet, reads the
 * contract's challenge window, then reads outstanding and a subject's redeemed
 * total for one channel. Pass a real channel id and a Discord guild and user to
 * inspect a specific channel:
 *
 *   CHANNEL_ID=0x... GUILD_ID=123 USER_ID=456 npm run example:read-live
 *
 * With no CHANNEL_ID it derives a sample id from a payer, a service and a salt.
 * That channel most likely does not exist yet, so its reads come back as zero,
 * which is itself a truthful read of the chain.
 */
import { formatUnits, getAddress, type Address, type Hex } from "viem";
import {
  ADDRESSES,
  ARC_CHAIN_ID,
  ARC_EXPLORER_URL,
  NanopayClient,
  USDC_DECIMALS,
  channelId as deriveChannelId,
  subjectId,
} from "../src/index.js";

const usdc = (atomic: bigint): string => `${formatUnits(atomic, USDC_DECIMALS)} USDC`;

async function main(): Promise<void> {
  const client = new NanopayClient();

  const chainId = await client.publicClient.getChainId();
  if (chainId !== ARC_CHAIN_ID) {
    throw new Error(`connected to chain ${chainId}, expected ${ARC_CHAIN_ID}`);
  }
  console.log(`Arc mainnet, chain id ${chainId}`);
  console.log(`NanoChannel ${ADDRESSES.nanoChannel}`);
  console.log(`${ARC_EXPLORER_URL}/address/${ADDRESSES.nanoChannel}\n`);

  const challengeWindow = await client.challengeWindow();
  console.log(`challengeWindow: ${challengeWindow} seconds`);

  const guildId = process.env.GUILD_ID ?? "111111111111111111";
  const userId = process.env.USER_ID ?? "222222222222222222";
  const subject = subjectId(guildId, userId);

  const cid: Hex =
    (process.env.CHANNEL_ID as Hex | undefined) ??
    deriveChannelId(
      getAddress("0xdb6c6340342e71a63cd11ebac2185204b7777777") as Address,
      getAddress("0x00000000000000000000000000000000000000AA") as Address,
      "0x0000000000000000000000000000000000000000000000000000000000000001",
    );

  console.log(`\nchannelId: ${cid}`);
  console.log(`subject discord:${guildId}:${userId} -> ${subject}`);

  const state = await client.channelOf(cid);
  const zero = "0x0000000000000000000000000000000000000000";
  if (state.payer.toLowerCase() === zero) {
    console.log("\nThis channel does not exist on chain yet. Reads below are zero.");
  } else {
    console.log(`\npayer ${state.payer}`);
    console.log(`service ${state.service}`);
    console.log(`deposit ${usdc(state.deposit)}`);
    console.log(`redeemed ${usdc(state.redeemed)}`);
    console.log(`guarded ${state.guarded}`);
  }

  const outstanding = await client.outstanding(cid);
  const subjectRedeemed = await client.subjectRedeemed(cid, subject);
  console.log(`\noutstanding: ${usdc(outstanding)}`);
  console.log(`subjectRedeemed: ${usdc(subjectRedeemed)}`);

  if (state.guarded) {
    const cap = await client.capOf(cid, subject);
    if (cap.set) {
      console.log(`cap: ${usdc(cap.limit)} per ${cap.window === 0n ? "lifetime" : `${cap.window}s`}`);
      console.log(`remaining: ${usdc(await client.remaining(cid, subject))}`);
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
