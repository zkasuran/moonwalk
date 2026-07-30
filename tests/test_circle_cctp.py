"""Tests for src/circle/cctp.py and the Gateway reader: calldata, plans, Iris.

Offline. Every chain read goes through a stand-in for ChainReader and every HTTP
call is served by pytest-httpx, so nothing here can reach an RPC or Circle.

The values checked are not invented. Selectors, domains, addresses and the Iris
fee shapes were verified against the live chains and the live API on 2026-07-29
(ARC-FACTS-2026-07-29.md), and the two attested-message samples come from the real
transfers recorded in docs/CIRCLE-INTEGRATIONS.md.
"""

from __future__ import annotations

from typing import Any

import pytest
from eth_abi.abi import decode as abi_decode
from web3 import Web3

from src.circle import cctp
from src.circle.gateway import GATEWAY_API_TESTNET, GatewayReader

ARC = cctp.chain("arc-testnet")
ETH = cctp.chain("eth-sepolia")
BASE = cctp.chain("base-sepolia")

SENDER = "0xDB6c6340342e71A63cD11Ebac2185204b7777777"
RECIPIENT = "0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4"

IRIS = cctp.IRIS_SANDBOX_URL

# Selectors from ARC-FACTS-2026-07-29.md, computed there from the signatures and
# cross-checked against the deployed contracts' dispatch tables.
DEPOSIT_FOR_BURN_SELECTOR = "0x8e0250ee"
RECEIVE_MESSAGE_SELECTOR = "0x57ecfd28"
APPROVE_SELECTOR = "0x095ea7b3"


class FakeReader:
    """A ChainReader with the numbers written down instead of read from a chain."""

    def __init__(
        self,
        *,
        balance: int = 0,
        allowance: int = 0,
        domain: int = 0,
        local_token: str = "0x" + "0" * 40,
    ) -> None:
        self._balance = balance
        self._allowance = allowance
        self._domain = domain
        self._local_token = local_token

    def usdc_balance(self, address: str) -> int:
        return self._balance

    def allowance(self, owner: str, spender: str) -> int:
        return self._allowance

    def local_domain(self) -> int:
        return self._domain

    def local_token(self, remote_domain: int, remote_token: str) -> str:
        return self._local_token


def bridge(
    *,
    source: cctp.ChainConfig = ETH,
    destination: cctp.ChainConfig = ARC,
    source_balance: int = 5_000_000,
    allowance: int = 0,
    destination_balance: int = 0,
    destination_token: str | None = None,
) -> cctp.CctpBridge:
    return cctp.CctpBridge(
        source,
        destination,
        source_reader=FakeReader(balance=source_balance, allowance=allowance, domain=source.domain),
        destination_reader=FakeReader(
            balance=destination_balance,
            domain=destination.domain,
            local_token=destination_token or destination.usdc,
        ),
    )


def fee_rows(source: int, destination: int, httpx_mock: Any, fast: float = 1.0) -> None:
    httpx_mock.add_response(
        url=f"{IRIS}/v2/burn/USDC/fees/{source}/{destination}",
        json=[
            {"finalityThreshold": 1000, "minimumFee": fast},
            {"finalityThreshold": 2000, "minimumFee": 0},
        ],
    )


# ---- the constants -------------------------------------------------------


def test_domains_and_addresses_are_the_verified_ones() -> None:
    assert (ARC.domain, ETH.domain, BASE.domain) == (26, 0, 6)
    assert (ARC.chain_id, ETH.chain_id, BASE.chain_id) == (5042002, 11155111, 84532)
    assert ARC.usdc == "0x3600000000000000000000000000000000000000"
    assert ETH.usdc == "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    assert BASE.usdc == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    # Arc pays gas in USDC, which is why a mint on Arc needs no ETH anywhere.
    assert ARC.gas_token == "USDC"
    assert ETH.gas_token == BASE.gas_token == "ETH"


def test_selectors_match_the_deployed_contracts() -> None:
    assert cctp.selector(cctp.DEPOSIT_FOR_BURN) == DEPOSIT_FOR_BURN_SELECTOR
    assert cctp.selector(cctp.RECEIVE_MESSAGE) == RECEIVE_MESSAGE_SELECTOR
    assert cctp.selector(cctp.APPROVE) == APPROVE_SELECTOR


def test_finality_has_exactly_two_levels() -> None:
    assert int(cctp.FinalityThreshold.FAST) == 1000
    assert int(cctp.FinalityThreshold.STANDARD) == 2000


def test_unknown_chain_names_the_ones_it_knows() -> None:
    with pytest.raises(KeyError) as caught:
        cctp.chain("arc-mainnet")
    assert "arc-testnet" in str(caught.value)


def test_address_to_bytes32_left_pads() -> None:
    padded = cctp.address_to_bytes32(RECIPIENT)
    assert len(padded) == 32
    assert padded[:12] == bytes(12)
    assert padded[12:].hex() == RECIPIENT[2:].lower()


# ---- calldata ------------------------------------------------------------


def test_deposit_for_burn_calldata_decodes_back_to_the_plan() -> None:
    call = cctp.build_deposit_for_burn(
        ETH, ARC, amount=1_000_000, recipient=RECIPIENT, max_fee=125, finality=1000
    )
    assert call.to == Web3.to_checksum_address(cctp.TOKEN_MESSENGER_V2)
    assert call.selector == DEPOSIT_FOR_BURN_SELECTOR
    decoded = abi_decode(
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        bytes.fromhex(call.data[10:]),
    )
    assert decoded[0] == 1_000_000
    assert decoded[1] == 26
    assert bytes(decoded[2]) == cctp.address_to_bytes32(RECIPIENT)
    assert Web3.to_checksum_address(str(decoded[3])) == Web3.to_checksum_address(ETH.usdc)
    # A zero destinationCaller is what lets the agent's own Arc wallet mint.
    assert bytes(decoded[4]) == bytes(32)
    assert decoded[5] == 125
    assert decoded[6] == 1000


def test_deposit_for_burn_can_pin_the_destination_caller() -> None:
    call = cctp.build_deposit_for_burn(
        ETH,
        ARC,
        amount=1,
        recipient=RECIPIENT,
        max_fee=1,
        destination_caller=SENDER,
    )
    decoded = abi_decode(
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        bytes.fromhex(call.data[10:]),
    )
    assert bytes(decoded[4]) == cctp.address_to_bytes32(SENDER)


def test_approve_targets_the_token_messenger() -> None:
    call = cctp.build_approve(ETH, 250_000)
    assert call.to == Web3.to_checksum_address(ETH.usdc)
    assert call.selector == APPROVE_SELECTOR
    spender, amount = abi_decode(["address", "uint256"], bytes.fromhex(call.data[10:]))
    assert Web3.to_checksum_address(str(spender)) == Web3.to_checksum_address(
        cctp.TOKEN_MESSENGER_V2
    )
    assert amount == 250_000


def test_receive_message_carries_iris_bytes_unchanged() -> None:
    message = "0x" + "ab" * 376
    attestation = "0x" + "cd" * 130
    call = cctp.build_receive_message(ARC, message=message, attestation=attestation)
    assert call.to == Web3.to_checksum_address(cctp.MESSAGE_TRANSMITTER_V2)
    assert call.selector == RECEIVE_MESSAGE_SELECTOR
    decoded = abi_decode(["bytes", "bytes"], bytes.fromhex(call.data[10:]))
    assert "0x" + bytes(decoded[0]).hex() == message
    assert "0x" + bytes(decoded[1]).hex() == attestation
    assert call.args == {"messageBytes": "376", "attestationBytes": "130"}


# ---- fees ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("bps", "amount", "expected"),
    [
        (1.0, 1_000_000, 100),
        (1.3, 1_000_000, 130),
        # Iris quotes fractional basis points, so rounding has to go up or the
        # burn is never attested.
        (1.3, 1_500_000, 195),
        (1.3, 1_001, 1),
        (0.0, 2_000_000, 0),
    ],
)
def test_fee_rounds_up(bps: float, amount: int, expected: int) -> None:
    assert cctp.FeeOption(1000, bps).fee_for(amount) == expected


def test_fee_lookup_refuses_an_unknown_finality(httpx_mock: Any) -> None:
    fee_rows(0, 26, httpx_mock)
    iris = cctp.IrisClient()
    with pytest.raises(RuntimeError) as caught:
        iris.fee_for(0, 26, 1500)
    assert "1000" in str(caught.value)


# ---- the refill decision -------------------------------------------------


def test_plan_tops_up_exactly_the_deficit(httpx_mock: Any) -> None:
    fee_rows(0, 26, httpx_mock)
    plan = bridge(destination_balance=9_258_000).plan(
        sender=SENDER, recipient=RECIPIENT, threshold=10_000_000
    )
    assert plan.deficit == 742_000
    assert plan.amount == 742_000
    assert plan.needed is True
    assert plan.funded is True
    assert plan.needs_approval is True
    # 1 bps on 0.742 USDC is 75 units, plus the 25 percent buffer.
    assert plan.max_fee == 94
    assert plan.minimum_received == 742_000 - 94
    assert [call.label for call in plan.calls] == ["approve", "depositForBurn"]


def test_plan_does_nothing_when_the_balance_is_healthy(httpx_mock: Any) -> None:
    fee_rows(0, 26, httpx_mock)
    plan = bridge(destination_balance=25_000_000).plan(sender=SENDER, threshold=10_000_000)
    assert plan.deficit == 0
    assert plan.amount == 0
    assert plan.needed is False
    assert plan.max_fee == 0
    assert plan.calls == []


def test_plan_skips_the_approval_when_the_allowance_is_there(httpx_mock: Any) -> None:
    fee_rows(0, 26, httpx_mock)
    plan = bridge(allowance=5_000_000).plan(sender=SENDER, threshold=1_000_000, amount=1_000_000)
    assert plan.needs_approval is False
    assert [call.label for call in plan.calls] == ["depositForBurn"]


def test_plan_knows_when_the_source_cannot_cover_it(httpx_mock: Any) -> None:
    fee_rows(0, 26, httpx_mock)
    plan = bridge(source_balance=100_000).plan(sender=SENDER, threshold=10_000_000)
    assert plan.funded is False


def test_plan_honours_an_explicit_max_fee(httpx_mock: Any) -> None:
    fee_rows(0, 26, httpx_mock)
    plan = bridge().plan(sender=SENDER, threshold=1_000_000, amount=1_000_000, max_fee=5)
    assert plan.max_fee == 5


def test_a_route_must_not_bridge_to_itself() -> None:
    with pytest.raises(ValueError):
        cctp.CctpBridge(ARC, ARC)


def test_route_check_passes_when_both_chains_agree() -> None:
    check = bridge().verify_route()
    assert check.domains_match is True
    assert check.mint_route_exists is True
    assert check.ok is True


def test_route_check_fails_on_an_unknown_mint_route() -> None:
    check = bridge(destination_token="0x" + "0" * 40).verify_route()
    assert check.mint_route_exists is False
    assert check.ok is False


def test_route_check_fails_when_the_rpc_is_a_different_chain() -> None:
    """A source RPC pointed at the wrong network would burn into the void, so the
    domain the chain reports is checked against the one we planned for."""
    wrong = cctp.CctpBridge(
        ETH,
        ARC,
        source_reader=FakeReader(domain=6),
        destination_reader=FakeReader(domain=26, local_token=ARC.usdc),
    )
    check = wrong.verify_route()
    assert check.domains_match is False
    assert check.ok is False


# ---- Iris ----------------------------------------------------------------

BURN_TX = "0xb130df607e2522617352f05b1bcb5fa54b7a81403b9816250dd005d4d5228ded"


def message_url(domain: int = 0, tx: str = BURN_TX) -> str:
    return f"{IRIS}/v2/messages/{domain}?transactionHash={tx}"


def test_a_404_means_not_indexed_yet(httpx_mock: Any) -> None:
    """Circle's own guide says a 404 is normal for the first few polls."""
    httpx_mock.add_response(url=message_url(), status_code=404, json={"error": "Message not found"})
    assert cctp.IrisClient().message(0, BURN_TX) is None


def test_a_pending_attestation_is_not_complete(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=message_url(),
        json={
            "messages": [
                {
                    "status": "pending_confirmations",
                    "message": "0x1234",
                    "attestation": "PENDING",
                    "eventNonce": "",
                    "cctpVersion": 2,
                }
            ]
        },
    )
    found = cctp.IrisClient().message(0, BURN_TX)
    assert found is not None
    assert found.status == "pending_confirmations"
    assert found.complete is False


def complete_body(message: str = "0xabcd", attestation: str = "0xdead") -> dict[str, Any]:
    return {
        "messages": [
            {
                "status": "complete",
                "message": message,
                "attestation": attestation,
                "eventNonce": "0x" + "7a" * 32,
                "cctpVersion": 2,
            }
        ]
    }


def test_waiting_polls_until_iris_signs(httpx_mock: Any) -> None:
    httpx_mock.add_response(url=message_url(), status_code=404, json={"error": "not found"})
    httpx_mock.add_response(url=message_url(), json=complete_body())
    slept: list[float] = []
    iris = cctp.IrisClient(poll_seconds=5.0)
    found = iris.wait_for_attestation(0, BURN_TX, sleep=slept.append)
    assert found.complete is True
    assert found.event_nonce == "0x" + "7a" * 32
    # One sleep, at the documented 5 second interval, between the two polls.
    assert slept == [5.0]


def test_waiting_gives_up_and_says_what_it_saw(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=message_url(),
        json={"messages": [{"status": "pending_confirmations", "attestation": "PENDING"}]},
        is_reusable=True,
    )
    iris = cctp.IrisClient(poll_seconds=0.0)
    with pytest.raises(TimeoutError) as caught:
        iris.wait_for_attestation(0, BURN_TX, timeout=0.0, sleep=lambda _: None)
    assert "pending_confirmations" in str(caught.value)


def test_burn_fees_and_allowance_come_from_iris(httpx_mock: Any) -> None:
    fee_rows(6, 26, httpx_mock, fast=1.3)
    httpx_mock.add_response(
        url=f"{IRIS}/v2/fastBurn/USDC/allowance",
        json={"allowance": 99999999991.52806, "lastUpdated": "2026-07-29T15:30:02.522Z"},
    )
    iris = cctp.IrisClient()
    assert iris.fee_for(6, 26, 1000).minimum_fee_bps == 1.3
    assert iris.fast_burn_allowance() > 1_000_000


# ---- is the attested message ours ----------------------------------------


def sample_message() -> bytearray:
    """A 376 byte CCTP V2 message with a burn body, patterned so any byte swap
    shows up. Length and field offsets match the live messages."""
    raw = bytearray((index * 7 + 3) % 256 for index in range(376))
    raw[12:44] = bytes(32)  # nonce, zero until Iris assigns it
    raw[144:148] = bytes(4)  # finalityThresholdExecuted
    raw[cctp.FEE_EXECUTED_OFFSET : cctp.FEE_EXECUTED_OFFSET + 32] = bytes(32)
    raw[344:376] = bytes(32)  # expirationBlock
    return raw


def attested_from(logged: bytearray, *, fee: int = 100) -> bytearray:
    attested = bytearray(logged)
    attested[12:44] = bytes.fromhex("c1" * 32)
    attested[144:148] = (1000).to_bytes(4, "big")
    attested[cctp.FEE_EXECUTED_OFFSET : cctp.FEE_EXECUTED_OFFSET + 32] = fee.to_bytes(32, "big")
    attested[344:376] = (54_459_964).to_bytes(32, "big")
    return attested


def hexed(raw: bytearray) -> str:
    return "0x" + bytes(raw).hex()


def test_the_fields_iris_fills_in_do_not_count_as_a_mismatch() -> None:
    logged = sample_message()
    assert cctp.attested_message_matches(hexed(logged), hexed(attested_from(logged))) is True


def test_a_changed_recipient_is_a_mismatch() -> None:
    """The point of the check: a message for a different recipient must not pass."""
    logged = sample_message()
    attested = attested_from(logged)
    attested[76] ^= 0xFF  # inside the recipient field
    assert cctp.attested_message_matches(hexed(logged), hexed(attested)) is False


def test_a_different_length_is_a_mismatch() -> None:
    logged = sample_message()
    assert cctp.attested_message_matches(hexed(logged), hexed(logged)[:-2]) is False


def test_fee_executed_is_read_out_of_the_message() -> None:
    logged = sample_message()
    assert cctp.fee_executed(hexed(attested_from(logged, fee=130))) == 130
    assert cctp.fee_executed(hexed(logged)) == 0


# ---- Gateway, read only --------------------------------------------------
#
# Lives here because it is the other Circle HTTP API this package talks to, and
# the read path is the whole module. There is no Gateway write path to test.


def test_gateway_balance_parses_circles_decimal_answer(httpx_mock: Any) -> None:
    """Gateway answers in decimal USDC, MoonWalk counts in atomic units."""
    httpx_mock.add_response(
        url=f"{GATEWAY_API_TESTNET}/v1/balances",
        json={
            "token": "USDC",
            "balances": [
                {
                    "domain": 26,
                    "depositor": RECIPIENT,
                    "balance": "0.001000",
                    "pendingBatch": "0.060300",
                }
            ],
        },
    )
    balance = GatewayReader().api_balance(RECIPIENT)
    assert balance.domain == 26
    assert balance.available_atomic == 1_000
    assert balance.pending_atomic == 60_300


def test_gateway_balance_refuses_an_empty_answer(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=f"{GATEWAY_API_TESTNET}/v1/balances", json={"token": "USDC", "balances": []}
    )
    with pytest.raises(RuntimeError):
        GatewayReader().api_balance(RECIPIENT)
