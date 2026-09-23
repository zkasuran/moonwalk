import {
  encodeAbiParameters,
  getAddress,
  keccak256,
  stringToBytes,
  type Address,
  type Hex,
} from "viem";

/**
 * The bytes32 subject for one Discord user in one server.
 *
 * A subject is what a spend is booked against. The agent holds one wallet, so
 * each person needs a stable identifier that is not an address. This hashes the
 * platform, the server and the user id together, the same way SpendGuard expects.
 * Anyone with the ids can recompute it and audit that person's spend.
 */
export function subjectId(guildId: string, userId: string): Hex {
  return keccak256(stringToBytes(`discord:${guildId}:${userId}`));
}

/** The human-readable preimage of a subject. For logs and receipts, never sent on chain. */
export function subjectLabel(guildId: string, userId: string): string {
  return `discord:${guildId}:${userId}`;
}

/**
 * The deterministic channel id: keccak256(abi.encode(payer, service, salt)).
 *
 * Derived the same way on chain (NanoChannel.channelIdOf) and here, so either
 * side can address a channel with no RPC call. Addresses are checksummed before
 * hashing so the encoding matches abi.encode regardless of input casing.
 */
export function channelId(payer: Address, service: Address, salt: Hex): Hex {
  return keccak256(
    encodeAbiParameters(
      [{ type: "address" }, { type: "address" }, { type: "bytes32" }],
      [getAddress(payer), getAddress(service), salt],
    ),
  );
}
