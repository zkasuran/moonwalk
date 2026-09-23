import { describe, expect, it } from "vitest";
import {
  encodeAbiParameters,
  keccak256,
  concat,
  recoverTypedDataAddress,
  type Address,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import {
  ADDRESSES,
  ARC_CHAIN_ID,
  NANO_CHANNEL_DOMAIN,
  OPEN_TYPE_STRING,
  OPEN_TYPES,
  ZERO_ADDRESS,
  openDigest,
  openTypedData,
  type DomainConfig,
} from "../src/index.js";

const cfg: DomainConfig = {
  chainId: ARC_CHAIN_ID,
  nanoChannel: ADDRESSES.nanoChannel,
  usdc: ADDRESSES.usdc,
};

const message = {
  service: "0x00000000000000000000000000000000000000AA" as Address,
  salt: "0x3333333333333333333333333333333333333333333333333333333333333333" as Hex,
  guarded: true,
  capOwner: ZERO_ADDRESS,
  deposit: 5_000_000n,
  capLimit: 1_000_000n,
  capWindow: 86_400n,
  authNonce: "0x4444444444444444444444444444444444444444444444444444444444444444" as Hex,
};

function utf8(value: string): Hex {
  return `0x${Buffer.from(value, "utf8").toString("hex")}` as Hex;
}

function openDigestByHand(): Hex {
  const domainTypehash = keccak256(
    utf8("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
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
        keccak256(utf8(NANO_CHANNEL_DOMAIN.name)),
        keccak256(utf8(NANO_CHANNEL_DOMAIN.version)),
        BigInt(cfg.chainId),
        cfg.nanoChannel,
      ],
    ),
  );
  const structHash = keccak256(
    encodeAbiParameters(
      [
        { type: "bytes32" },
        { type: "address" },
        { type: "bytes32" },
        { type: "bool" },
        { type: "address" },
        { type: "uint256" },
        { type: "uint256" },
        { type: "uint64" },
        { type: "bytes32" },
      ],
      [
        keccak256(utf8(OPEN_TYPE_STRING)),
        message.service,
        message.salt,
        message.guarded,
        message.capOwner,
        message.deposit,
        message.capLimit,
        message.capWindow,
        message.authNonce,
      ],
    ),
  );
  return keccak256(concat(["0x1901", domainSeparator, structHash]));
}

describe("open digest", () => {
  it("uses the Open field order the contract expects", () => {
    expect(OPEN_TYPES.Open.map((f) => f.name)).toEqual([
      "service",
      "salt",
      "guarded",
      "capOwner",
      "deposit",
      "capLimit",
      "capWindow",
      "authNonce",
    ]);
  });

  it("matches an independently computed EIP-712 digest", () => {
    expect(openDigest(cfg, message)).toBe(openDigestByHand());
  });

  it("a signed Open recovers to the payer", async () => {
    const account = privateKeyToAccount(
      "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    );
    const typed = openTypedData(cfg, message);
    const signature = await account.signTypedData(typed);
    const recovered = await recoverTypedDataAddress({ ...typed, signature });
    expect(recovered).toBe(account.address);
  });
});
