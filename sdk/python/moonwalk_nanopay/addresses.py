"""Arc mainnet addresses, chain ids and EIP-712 domains for the MoonWalk contracts.

Every amount this SDK touches is USDC atomic units, the 6 decimal ERC-20 view.
Gas on Arc is paid in USDC through an 18 decimal native balance. The 18 decimal
view appears nowhere here, because mixing the two is the classic Arc decimals bug.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---- chain ------------------------------------------------------------------

ARC_CHAIN_ID = 5042
ARC_RPC_URL = "https://rpc.mainnet.arc.io"
ARC_EXPLORER_URL = "https://explorer.arc.io"

#: USDC uses 6 decimals for its ERC-20 view and so does every amount here.
USDC_DECIMALS = 6

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# ---- live Arc mainnet addresses ---------------------------------------------
# Verified against deployments/arc-mainnet.json (chain 5042).

USDC_ADDRESS = "0x3600000000000000000000000000000000000000"
NANO_CHANNEL_ADDRESS = "0x059D3A87E91fA91D341f364868A9Ed333077989a"
SPEND_GUARD_ADDRESS = "0xAbB85ab157357676eBE7ae17A161168912A3c232"
SERVICE_REGISTRY_ADDRESS = "0x1b9FF1FAD0181705B750C325B3A82137bB153866"


@dataclass(frozen=True)
class Addresses:
    """The set of contract addresses a client talks to. Defaults to Arc mainnet."""

    usdc: str = USDC_ADDRESS
    nano_channel: str = NANO_CHANNEL_ADDRESS
    spend_guard: str = SPEND_GUARD_ADDRESS
    service_registry: str = SERVICE_REGISTRY_ADDRESS


#: Live Arc mainnet deployment, the default for a fresh client.
MAINNET_ADDRESSES = Addresses()

# ---- EIP-712 domains --------------------------------------------------------

#: USDC EIP-712 domain, for the EIP-3009 and EIP-2612 signatures.
USDC_DOMAIN_NAME = "USDC"
USDC_DOMAIN_VERSION = "2"

#: NanoChannel EIP-712 domain, for the Open, Voucher and Close signatures.
NANO_CHANNEL_DOMAIN_NAME = "MoonWalk NanoChannel"
NANO_CHANNEL_DOMAIN_VERSION = "1"

# ---- EIP-712 type strings and the on-chain typehashes -----------------------
# The type strings are the exact ones the contracts hash. The typehash constants
# are the values NanoChannel stores on chain, quoted here so the tests can assert
# the SDK computes the same keccak256 the contract does.

OPEN_TYPE_STRING = (
    "Open(address service,bytes32 salt,bool guarded,address capOwner,"
    "uint256 deposit,uint256 capLimit,uint64 capWindow,bytes32 authNonce)"
)

VOUCHER_TYPE_STRING = (
    "Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)"
)

CLOSE_TYPE_STRING = "Close(bytes32 channelId,uint256 redeemed)"

RECEIVE_WITH_AUTHORIZATION_TYPE_STRING = (
    "ReceiveWithAuthorization(address from,address to,uint256 value,"
    "uint256 validAfter,uint256 validBefore,bytes32 nonce)"
)

#: On-chain NanoChannel.OPEN_TYPEHASH. Asserted equal to keccak256(OPEN_TYPE_STRING).
ONCHAIN_OPEN_TYPEHASH = "0x9bd5e1d916549fd486a56398e9a2230b47e1d1f8e6887cbeffc06ced82cf1959"

#: On-chain NanoChannel.VOUCHER_TYPEHASH. Asserted equal to keccak256(VOUCHER_TYPE_STRING).
ONCHAIN_VOUCHER_TYPEHASH = "0x1938bd40683f855f94a3d72f5781fd5efefa46edf4784508f4f202211b7cc140"

# ---- default TTLs -----------------------------------------------------------

#: How long a deposit authorization stays usable if no validBefore is given.
DEFAULT_DEPOSIT_TTL_SECONDS = 3600

#: How long a voucher stays redeemable if no validBefore is given.
DEFAULT_VOUCHER_TTL_SECONDS = 86_400


def tx_url(tx_hash: str) -> str:
    """Explorer link for a transaction."""
    h = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    return f"{ARC_EXPLORER_URL}/tx/{h}"


def address_url(address: str) -> str:
    """Explorer link for an address."""
    return f"{ARC_EXPLORER_URL}/address/{address}"
