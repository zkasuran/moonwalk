import { describe, expect, it, vi } from "vitest";
import { decodeFunctionData, type Address, type Hex, type PublicClient } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { NanopayClient, channelId as deriveChannelId, nanoChannelAbi } from "../src/index.js";

const NANO = "0x059D3A87E91fA91D341f364868A9Ed333077989a";

const payer = privateKeyToAccount(
  "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
);
const service = "0x00000000000000000000000000000000000000AA" as Address;
const salt = "0x3333333333333333333333333333333333333333333333333333333333333333" as Hex;
const subject = "0x2222222222222222222222222222222222222222222222222222222222222222" as Hex;

// A public client that only answers readContract, keyed by the function name.
// Enough to exercise every read the SDK maps, with no network.
function mockPublicClient(): PublicClient {
  const readContract = vi.fn(async ({ functionName }: { functionName: string }) => {
    switch (functionName) {
      case "channelOf":
        return {
          payer: payer.address,
          service,
          deposit: 5_000_000n,
          redeemed: 1_500_000n,
          closeAt: 0n,
          guarded: true,
          settled: false,
        };
      case "outstanding":
        return 3_500_000n;
      case "subjectRedeemed":
        return 1_500_000n;
      case "challengeWindow":
        return 3600n;
      case "capOf":
        return [1_000_000n, 86_400n, true];
      case "remaining":
        return 250_000n;
      case "usageOf":
        return [750_000n, 1_790_000_000n];
      case "balanceOf":
        return 42_000_000n;
      default:
        throw new Error(`unexpected read ${functionName}`);
    }
  });
  return { readContract } as unknown as PublicClient;
}
describe("NanopayClient reads (mocked RPC)", () => {
  const client = new NanopayClient({ publicClient: mockPublicClient() });
  const cid = deriveChannelId(payer.address, service, salt);

  it("maps channelOf into a typed ChannelState", async () => {
    const state = await client.channelOf(cid);
    expect(state.payer).toBe(payer.address);
    expect(state.deposit).toBe(5_000_000n);
    expect(state.redeemed).toBe(1_500_000n);
    expect(state.guarded).toBe(true);
    expect(state.settled).toBe(false);
  });

  it("reads outstanding and subjectRedeemed as bigints", async () => {
    expect(await client.outstanding(cid)).toBe(3_500_000n);
    expect(await client.subjectRedeemed(cid, subject)).toBe(1_500_000n);
  });

  it("maps SpendGuard capOf and usageOf", async () => {
    const cap = await client.capOf(cid, subject);
    expect(cap).toEqual({ limit: 1_000_000n, window: 86_400n, set: true });
    expect(await client.remaining(cid, subject)).toBe(250_000n);
    const usage = await client.usageOf(cid, subject);
    expect(usage).toEqual({ used: 750_000n, windowStart: 1_790_000_000n });
  });

  it("reads a USDC balance", async () => {
    expect(await client.usdcBalance(payer.address)).toBe(42_000_000n);
  });
});

describe("NanopayClient calldata builders", () => {
  const client = new NanopayClient({ publicClient: mockPublicClient() });

  it("openChannel signs both messages and encodes open()", async () => {
    const result = await client.openChannel({
      payer,
      service,
      salt,
      guarded: true,
      deposit: 5_000_000n,
      capLimit: 1_000_000n,
      capWindow: 86_400n,
      validBefore: 1_800_000_000n,
      nonce: "0x4444444444444444444444444444444444444444444444444444444444444444",
    });
    expect(result.to.toLowerCase()).toBe(NANO.toLowerCase());
    expect(result.channelId).toBe(deriveChannelId(payer.address, service, salt));
    expect(result.authorization.from).toBe(payer.address);
    expect(result.authorization.value).toBe(5_000_000n);
    expect(result.openSignature.startsWith("0x")).toBe(true);

    const decoded = decodeFunctionData({ abi: nanoChannelAbi, data: result.data });
    expect(decoded.functionName).toBe("open");
    const args = decoded.args as readonly unknown[];
    expect(args[0]).toBe(service);
    expect(args[2]).toBe(true);
    expect(args[4]).toBe(1_000_000n);
    const auth = args[6] as { from: Address; value: bigint };
    expect(auth.from).toBe(payer.address);
    expect(auth.value).toBe(5_000_000n);
  });

  it("redeem encodes vouchers and signatures", async () => {
    const cid = deriveChannelId(payer.address, service, salt);
    const voucher = client.voucher(cid, subject, 2_000_000n, { validBefore: 1_800_000_000n });
    const signature = await client.signVoucher(payer, voucher);
    const tx = client.redeem(cid, [{ voucher, signature }]);
    const decoded = decodeFunctionData({ abi: nanoChannelAbi, data: tx.data });
    expect(decoded.functionName).toBe("redeem");
    const args = decoded.args as readonly unknown[];
    expect(args[0]).toBe(cid);
    expect((args[1] as readonly unknown[]).length).toBe(1);
    expect((args[2] as readonly Hex[])[0]).toBe(signature);
  });

  it("requestClose, withdraw and closeMutual encode the right selectors", () => {
    const cid = deriveChannelId(payer.address, service, salt);
    expect(
      decodeFunctionData({ abi: nanoChannelAbi, data: client.requestClose(cid).data }).functionName,
    ).toBe("requestClose");
    expect(
      decodeFunctionData({ abi: nanoChannelAbi, data: client.withdraw(cid).data }).functionName,
    ).toBe("withdraw");
    const close = client.closeMutual(cid, "0xaa", "0xbb");
    expect(decodeFunctionData({ abi: nanoChannelAbi, data: close.data }).functionName).toBe(
      "closeMutual",
    );
  });

  it("submit throws without a wallet client", async () => {
    const cid = deriveChannelId(payer.address, service, salt);
    await expect(client.requestClose(cid).submit()).rejects.toThrow(/no wallet client/i);
  });
});
