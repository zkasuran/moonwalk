import { describe, expect, it } from "vitest";
import { keccak256, encodeAbiParameters, getAddress, stringToBytes, type Address, type Hex } from "viem";
import { channelId, subjectId, subjectLabel } from "../src/index.js";

/**
 * Known-answer tests for the two id helpers. The expected values were computed
 * with viem from the exact preimages the contracts use, so a change to either
 * helper that would break agreement with the chain fails here.
 */
describe("subjectId", () => {
  it("matches known keccak256 values", () => {
    expect(subjectId("123", "456")).toBe(
      "0xba5f2d41d5f8bd9684d4c5a9a8645658d2eb00aa9eb0f0ce4cc0e89f9defe9c1",
    );
    expect(subjectId("123456789012345678", "987654321098765432")).toBe(
      "0x2e2b521bafbec2ef50a277efbd911cf0e15c0afed2fc60bdad6ea51dc8f313c1",
    );
  });

  it("hashes the discord:<guildId>:<userId> preimage", () => {
    expect(subjectLabel("123", "456")).toBe("discord:123:456");
    expect(subjectId("123", "456")).toBe(keccak256(stringToBytes("discord:123:456")));
  });
});

describe("channelId", () => {
  const payer = getAddress("0xdb6c6340342e71a63cd11ebac2185204b7777777") as Address;
  const service = getAddress("0x00000000000000000000000000000000000000AA") as Address;
  const salt = "0x0000000000000000000000000000000000000000000000000000000000000001" as Hex;

  it("matches a known keccak256(abi.encode(payer, service, salt))", () => {
    expect(channelId(payer, service, salt)).toBe(
      "0x8c682615d8567d6946c8b4f01ceb2582509cdca93e9359592319e5d39a7a3cec",
    );
  });

  it("equals the raw abi.encode derivation", () => {
    const expected = keccak256(
      encodeAbiParameters(
        [{ type: "address" }, { type: "address" }, { type: "bytes32" }],
        [payer, service, salt],
      ),
    );
    expect(channelId(payer, service, salt)).toBe(expected);
  });

  it("is independent of input address casing", () => {
    const lower = channelId(
      payer.toLowerCase() as Address,
      service.toLowerCase() as Address,
      salt,
    );
    expect(lower).toBe(channelId(payer, service, salt));
  });
});
