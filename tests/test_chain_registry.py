"""Tests for src/chain/registry.py and the shared ArcClient helpers it leans on.

Nothing here needs a node. Three things get checked: the namespace hash the
registry addresses a community by, the pure listing logic the agent uses to decide
what it may buy and the plumbing in client.py that every contract client shares,
which is revert decoding, the gas fee conversion and the committed ABIs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from Crypto.Hash import keccak

from src.chain.client import SentTx, error_selectors, load_abi, revert_name
from src.chain.registry import ServiceListing, discord_namespace
from src.chain.subjects import SUBJECT_PREFIX, discord_subject

GUILD = "1517400111699726488"
OTHER_GUILD = "1113004055648059402"
ALICE = "402935800371413000"

CHAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "chain"
ABI_DIR = CHAIN_DIR / "abis"

# Every contract function the Python clients call, written out by hand. A contract
# change that is not followed by `make abis` fails here instead of at runtime.
REQUIRED_FUNCTIONS = {
    "NanoChannel": (
        "open topUp redeem closeMutual requestClose withdraw voucherHash closeHash "
        "channelIdOf channelOf outstanding subjectRedeemed challengeWindow"
    ).split(),
    "SpendGuard": (
        "registerScope setDefaultCap setSubjectCap consume capOf usageOf remaining scopeOwner"
    ).split(),
    "ServiceRegistry": (
        "serviceIdOf namespaceAdmin namespaceMaxPrice isBuyable getService idsOf claimNamespace "
        "register setVerified setEnabled setPrice setMaxPrice"
    ).split(),
    "USDC": ["balanceOf"],
}


def keccak256(data: bytes) -> bytes:
    """keccak-256 through pycryptodome, so the preimage and the primitive are both
    stated here rather than borrowed from the code under test."""
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return digest.digest()


def selector(signature: str) -> str:
    """The 4 byte selector of a custom error, computed the way the EVM does."""
    return "0x" + keccak256(signature.encode())[:4].hex()


def snake(name: str) -> str:
    """camelCase to snake_case, for comparing Solidity fields to dataclass ones."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def make_listing(**overrides: Any) -> ServiceListing:
    fields: dict[str, Any] = {
        "service_id": keccak256(b"weather"),
        "namespace": discord_namespace(GUILD),
        "lister": "0x0000000000000000000000000000000000000001",
        "pay_to": "0x0000000000000000000000000000000000000002",
        "asset": "0x3600000000000000000000000000000000000000",
        "price_atomic": 1_000,
        "verified": True,
        "enabled": True,
        "name": "weather",
        "description": "current conditions",
        "endpoint": "https://example.com/weather",
    }
    fields.update(overrides)
    return ServiceListing(**fields)


# ---- namespaces -----------------------------------------------------------


def test_namespace_is_keccak_of_the_documented_preimage() -> None:
    # ServiceRegistry.sol documents a namespace as keccak256("discord:<guildId>").
    assert discord_namespace(GUILD) == keccak256(f"discord:{GUILD}".encode())


def test_namespace_is_bytes32() -> None:
    value = discord_namespace(GUILD)
    assert isinstance(value, bytes)
    assert len(value) == 32


def test_namespace_is_stable_and_per_guild() -> None:
    assert discord_namespace(GUILD) == discord_namespace(GUILD)
    assert discord_namespace(GUILD) != discord_namespace(OTHER_GUILD)


def test_namespace_shares_the_prefix_with_subjects() -> None:
    # registry.py spells the prefix out again instead of importing it, so pin both
    # sides to the same literal and a change to one has to fail here.
    assert SUBJECT_PREFIX == "discord"
    assert discord_namespace(GUILD) == keccak256(f"{SUBJECT_PREFIX}:{GUILD}".encode())


def test_namespace_never_collides_with_a_subject() -> None:
    # Namespaces and subjects are both bytes32 under the same "discord:" prefix, so
    # what keeps them apart is the extra ":<userId>" segment in a subject preimage.
    # Discord ids are digits, so real input cannot forge the segment count.
    namespaces = {discord_namespace(g) for g in (GUILD, OTHER_GUILD, ALICE)}
    subjects = {discord_subject(g, u) for g in (GUILD, OTHER_GUILD) for u in (ALICE, GUILD)}
    assert namespaces.isdisjoint(subjects)


# ---- listings -------------------------------------------------------------


def test_a_listing_is_buyable_only_when_verified_and_enabled() -> None:
    # The admin's approval and the lister's switch both have to be on. This is the
    # check the agent makes before it spends.
    assert make_listing().buyable is True
    assert make_listing(verified=False).buyable is False
    assert make_listing(enabled=False).buyable is False
    assert make_listing(verified=False, enabled=False).buyable is False


def test_price_display_is_four_decimals_of_usdc() -> None:
    assert make_listing(price_atomic=1_000).price_display == "$0.0010"
    assert make_listing(price_atomic=250_000).price_display == "$0.2500"


def test_service_listing_reads_get_service_in_the_declared_order() -> None:
    # RegistryClient.get() fills the dataclass from the returned tuple by position,
    # so the Solidity field order and the dataclass field order are one thing. A
    # reordered struct would swap payTo for asset and still decode.
    abi = load_abi("ServiceRegistry")
    entry = next(i for i in abi if i.get("type") == "function" and i["name"] == "getService")
    fields = [str(c["name"]) for c in entry["outputs"][0]["components"]]
    expected = (
        "namespace lister payTo asset priceAtomic verified enabled name description endpoint"
    ).split()
    assert fields == expected
    declared = [f for f in ServiceListing.__dataclass_fields__ if f != "service_id"]
    assert [snake(f) for f in fields] == declared


# ---- revert decoding ------------------------------------------------------


def test_error_selectors_names_the_guard_refusal() -> None:
    # The over-cap voucher in the live run came back as this selector. It is why
    # the receipt says "CapExceeded" instead of "execution reverted".
    sel = selector("CapExceeded(bytes32,uint256,uint256,uint256)")
    assert error_selectors(load_abi("SpendGuard"))[sel] == "CapExceeded"


def test_error_selectors_covers_every_declared_error() -> None:
    for name in ("NanoChannel", "SpendGuard", "ServiceRegistry"):
        abi = load_abi(name)
        declared = [i for i in abi if i.get("type") == "error"]
        selectors = error_selectors(abi)
        assert len(selectors) == len(declared), f"{name} has two errors on one selector"
        assert set(selectors.values()) == {str(i["name"]) for i in declared}


def test_revert_name_decodes_a_full_revert_payload() -> None:
    # Real revert data is the selector then the encoded arguments, four words here:
    # subject, used, amount and limit.
    payload = selector("CapExceeded(bytes32,uint256,uint256,uint256)") + "00" * 128
    assert revert_name(payload, "NanoChannel", "SpendGuard") == "CapExceeded"


def test_revert_name_decodes_a_channel_error() -> None:
    payload = selector("StaleVoucher(bytes32,uint256,uint256)")
    assert revert_name(payload, "NanoChannel") == "StaleVoucher"
    # An ABI that does not declare it cannot name it, so the selector comes back.
    assert revert_name(payload, "SpendGuard") == payload


def test_revert_name_returns_the_selector_when_it_is_unknown() -> None:
    assert "0xdeadbeef" not in error_selectors(load_abi("NanoChannel"))
    assert revert_name("0xdeadbeef", "NanoChannel", "SpendGuard") == "0xdeadbeef"
    assert revert_name("0xDEADBEEF", "NanoChannel") == "0xdeadbeef"


def test_revert_name_is_unknown_without_usable_data() -> None:
    # Missing, empty or truncated data has to come back as "unknown" rather than
    # raise while we are already reporting a failure.
    assert revert_name(None, "NanoChannel") == "unknown"
    assert revert_name("", "NanoChannel") == "unknown"
    assert revert_name("0x", "NanoChannel") == "unknown"
    assert revert_name("0x1234", "NanoChannel") == "unknown"  # under four bytes
    assert revert_name("deadbeef12", "NanoChannel") == "unknown"  # no 0x prefix


# ---- gas ------------------------------------------------------------------


def test_gas_cost_atomic_converts_the_native_fee_to_the_usdc_view() -> None:
    # Gas on Arc is USDC through the native interface, which is 18 decimals, while
    # the ERC-20 view this package uses everywhere is 6. The divisor is 10 ** 12,
    # exactly that gap. Mixing the two is the classic Arc bug. The numbers are the
    # redeem from evidence/channel-20260729T154745Z.json: 262,639 gas at 25 gwei,
    # which the run recorded as a 6,565 atomic fee, so $0.006565.
    sent = SentTx(
        tx_hash="0xb779492a6c66abc1d98e4ca13786fd9b968843a9f10e07b0a27620c89f11767a",
        block_number=54_278_274,
        gas_used=262_639,
        status=1,
        effective_gas_price=25_000_000_000,
    )
    assert sent.gas_cost_atomic == 6_565
    assert sent.ok is True


def test_gas_cost_atomic_floors_a_sub_atomic_fee() -> None:
    # Under 10 ** 12 native units is less than one atomic unit of USDC, so it
    # truncates to zero instead of turning into a float.
    assert SentTx("0x0", 1, gas_used=1, status=1, effective_gas_price=1).gas_cost_atomic == 0
    # A receipt with no effectiveGasPrice reports no fee rather than raising.
    assert SentTx("0x0", 1, gas_used=21_000, status=1).gas_cost_atomic == 0


def test_ok_is_only_a_status_of_one() -> None:
    assert SentTx("0x0", 1, gas_used=1, status=1).ok is True
    assert SentTx("0x0", 1, gas_used=1, status=0).ok is False


# ---- committed ABIs -------------------------------------------------------


def test_every_committed_abi_loads() -> None:
    files = sorted(p.stem for p in ABI_DIR.glob("*.json"))
    assert files == sorted(REQUIRED_FUNCTIONS)
    for name in files:
        abi = load_abi(name)
        assert isinstance(abi, list)
        assert abi, f"src/chain/abis/{name}.json is empty"
        assert all(isinstance(item, dict) and "type" in item for item in abi)


def test_committed_abis_expose_the_functions_the_clients_call() -> None:
    for name, required in REQUIRED_FUNCTIONS.items():
        have = {str(i["name"]) for i in load_abi(name) if i.get("type") == "function"}
        missing = sorted(set(required) - have)
        assert not missing, (
            f"src/chain/abis/{name}.json is stale, it is missing {missing}. The runtime "
            "reads the committed ABI, so regenerate it after a contract change: make abis"
        )


def test_no_call_site_uses_a_function_its_abi_does_not_have() -> None:
    # The same staleness from the other end. Instead of trusting the list above,
    # read every `.functions.<name>` call site in src/chain and check it against the
    # ABI behind that handle.
    handles = {
        "channel": "NanoChannel",
        "guard": "SpendGuard",
        "registry": "ServiceRegistry",
        "usdc": "USDC",
    }
    functions = {
        abi_name: {str(i["name"]) for i in load_abi(abi_name) if i.get("type") == "function"}
        for abi_name in handles.values()
    }
    pattern = re.compile(r"\.(channel|guard|registry|usdc)\.functions\.([A-Za-z_]\w*)")
    seen = 0
    for path in sorted(CHAIN_DIR.glob("*.py")):
        for handle, called in pattern.findall(path.read_text()):
            seen += 1
            abi_name = handles[handle]
            assert called in functions[abi_name], (
                f"{path.name} calls {called} on {abi_name}, which is not in "
                f"src/chain/abis/{abi_name}.json. Regenerate it: make abis"
            )
    assert seen > 20, "the call site scan matched almost nothing, so it is not checking"
