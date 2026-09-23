import { describe, expect, it } from "vitest";
import { keccak256, stringToBytes } from "viem";
import {
  CLOSE_TYPE_STRING,
  ONCHAIN_OPEN_TYPEHASH,
  ONCHAIN_VOUCHER_TYPEHASH,
  OPEN_TYPE_STRING,
  RECEIVE_WITH_AUTHORIZATION_TYPE_STRING,
  VOUCHER_TYPE_STRING,
} from "../src/index.js";

/**
 * The point of the SDK: it agrees with the deployed contract byte for byte. The
 * typehash is keccak256 of the exact struct type string, so if these match the
 * values NanoChannel stores on chain, every signature the SDK builds will verify.
 */
describe("EIP-712 typehashes agree with the deployed NanoChannel", () => {
  it("OPEN_TYPEHASH equals the on-chain value", () => {
    const computed = keccak256(stringToBytes(OPEN_TYPE_STRING));
    expect(computed).toBe(
      "0x9bd5e1d916549fd486a56398e9a2230b47e1d1f8e6887cbeffc06ced82cf1959",
    );
    expect(computed).toBe(ONCHAIN_OPEN_TYPEHASH);
  });

  it("VOUCHER_TYPEHASH equals the on-chain value", () => {
    const computed = keccak256(stringToBytes(VOUCHER_TYPE_STRING));
    expect(computed).toBe(
      "0x1938bd40683f855f94a3d72f5781fd5efefa46edf4784508f4f202211b7cc140",
    );
    expect(computed).toBe(ONCHAIN_VOUCHER_TYPEHASH);
  });

  it("CLOSE_TYPEHASH matches keccak256 of its type string", () => {
    // The contract stores keccak256("Close(bytes32 channelId,uint256 redeemed)").
    const computed = keccak256(stringToBytes(CLOSE_TYPE_STRING));
    expect(computed).toBe(keccak256(stringToBytes("Close(bytes32 channelId,uint256 redeemed)")));
  });

  it("the EIP-3009 type string is the canonical ReceiveWithAuthorization", () => {
    // The USDC EIP-3009 ReceiveWithAuthorization typehash, fixed by the standard.
    const computed = keccak256(stringToBytes(RECEIVE_WITH_AUTHORIZATION_TYPE_STRING));
    expect(computed).toBe(
      "0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8",
    );
  });
});
