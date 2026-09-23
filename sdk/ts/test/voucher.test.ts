import { describe, expect, it } from "vitest";
import {
  encodeAbiParameters,
  keccak256,
  concat,
  recoverTypedDataAddress,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import {
  ADDRESSES,
  ARC_CHAIN_ID,
  NANO_CHANNEL_DOMAIN,
  VOUCHER_TYPE_STRING,
  VOUCHER_TYPES,
  voucherDigest,
  voucherTypedData,
  type DomainConfig,
} from "../src/index.js";

const cfg: DomainConfig = {
  chainId: ARC_CHAIN_ID,
  nanoChannel: ADDRESSES.nanoChannel,
  usdc: ADDRESSES.usdc,
};

const voucher = {
  channelId: "0x1111111111111111111111111111111111111111111111111111111111111111" as Hex,
  subject: "0x2222222222222222222222222222222222222222222222222222222222222222" as Hex,
  cumulative: 123_456n,
  validBefore: 1_800_000_000n,
};

/**
 * Rebuild the EIP-712 digest from the spec by hand, then check the SDK computes
 * the same thing. The SDK uses viem's hashTypedData, this test uses the raw
 * keccak256 pieces, so agreement means both encodings are right.
 */
function digestByHand(): Hex {
  const domainTypehash = keccak256(
    stringToUtf8("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
  );
  const domainSeparator = keccak256(
    encodeAbiParameters(
      [
        { type: "bytes32" },
        { type: "bytes32" },
        { type: "bytes32" },
        { type: "uint256" },
        { type: "address" },
      ],
      [
        domainTypehash,
        keccak256(stringToUtf8(NANO_CHANNEL_DOMAIN.name)),
        keccak256(stringToUtf8(NANO_CHANNEL_DOMAIN.version)),
        BigInt(cfg.chainId),
        cfg.nanoChannel,
      ],
    ),
  );
  const structHash = keccak256(
    encodeAbiParameters(
      [
        { type: "bytes32" },
        { type: "bytes32" },
        { type: "bytes32" },
        { type: "uint256" },
        { type: "uint64" },
      ],
      [
        keccak256(stringToUtf8(VOUCHER_TYPE_STRING)),
        voucher.channelId,
        voucher.subject,
        voucher.cumulative,
        voucher.validBefore,
      ],
    ),
  );
  return keccak256(concat(["0x1901", domainSeparator, structHash]));
}

function stringToUtf8(value: string): Hex {
  return `0x${Buffer.from(value, "utf8").toString("hex")}` as Hex;
}

describe("voucher digest", () => {
  it("matches an independently computed EIP-712 digest", () => {
    expect(voucherDigest(cfg, voucher)).toBe(digestByHand());
  });

  it("the typed data uses the Voucher field order the contract expects", () => {
    const typed = voucherTypedData(cfg, voucher);
    expect(typed.primaryType).toBe("Voucher");
    expect(VOUCHER_TYPES.Voucher.map((f) => f.name)).toEqual([
      "channelId",
      "subject",
      "cumulative",
      "validBefore",
    ]);
  });

  it("a signed voucher recovers to the signer", async () => {
    // A throwaway well-known test key. Never a real key.
    const account = privateKeyToAccount(
      "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    );
    const typed = voucherTypedData(cfg, voucher);
    const signature = await account.signTypedData(typed);
    const recovered = await recoverTypedDataAddress({ ...typed, signature });
    expect(recovered).toBe(account.address);
  });
});
