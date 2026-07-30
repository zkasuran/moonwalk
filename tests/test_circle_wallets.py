"""Tests for src/circle/wallets.py: the signer abstraction and the Circle backend.

Offline, all of it. No API key, no network, no .env. The Circle developer-controlled
wallet backend is exercised against a stand-in for Circle's SigningApi that signs
with a throwaway local key, so the request MoonWalk builds and the response it
parses are both covered without a credential.

The EIP-712 constants are checked against values read from the live chain on
2026-07-29 (ARC-FACTS-2026-07-29.md): Arc USDC's own DOMAIN_SEPARATOR() and the
EIP-3009 typehashes Circle publishes. A test that only compares our encoder to
itself would pass while the rail was broken.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace
from typing import Any

import pytest
from circle.web3 import developer_controlled_wallets as dcw
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.crypto import keccak

from src.circle import wallets

AGENT = Account.from_key("0x" + "a1" * 32)
CIRCLE = Account.from_key("0x" + "c1" * 32)

ARC_CHAIN_ID = 5042002
USDC = "0x3600000000000000000000000000000000000000"
CHANNEL = "0x3e2dE84eD534E39241682957d617ed761892D568"

# Read from the chain: `cast call 0x3600...0000 "DOMAIN_SEPARATOR()(bytes32)"`.
USDC_DOMAIN_SEPARATOR = bytes.fromhex(
    "361191522483d32a83e70ae7183b4b9629442c13a78bc9921d6f707911c8c6b0"
)
# Circle's published EIP-3009 typehashes, recomputed and matched against the live
# contract's behaviour.
TRANSFER_TYPEHASH = bytes.fromhex(
    "7c7c6cdb67a18743f49ec6fa9b35f50d52ed05cbed4cc592e13b44501c1a2267"
)
RECEIVE_TYPEHASH = bytes.fromhex("d099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8")

# A channel voucher in the shape src/chain/channel.py builds it: raw bytes for the
# bytes32 fields, which is exactly what the Circle backend has to cope with.
VOUCHER: wallets.TypedData = {
    "types": {
        "EIP712Domain": wallets.EIP712_DOMAIN,
        "Voucher": [
            {"name": "channelId", "type": "bytes32"},
            {"name": "subject", "type": "bytes32"},
            {"name": "cumulative", "type": "uint256"},
            {"name": "validBefore", "type": "uint64"},
        ],
    },
    "primaryType": "Voucher",
    "domain": {
        "name": "MoonWalk NanoChannel",
        "version": "1",
        "chainId": ARC_CHAIN_ID,
        "verifyingContract": CHANNEL,
    },
    "message": {
        "channelId": bytes.fromhex("11" * 32),
        "subject": bytes.fromhex("22" * 32),
        "cumulative": 30_000,
        "validBefore": 1_800_000_000,
    },
}


def digest(typed_data: wallets.TypedData) -> bytes:
    """The 32 bytes a signature covers, per EIP-712."""
    signable = encode_typed_data(full_message=typed_data)
    return keccak(b"\x19\x01" + signable.header + signable.body)


def type_string(name: str, fields: list[dict[str, str]]) -> str:
    inner = ",".join(f"{field['type']} {field['name']}" for field in fields)
    return f"{name}({inner})"


class FakeSigningApi:
    """Stands in for Circle's SigningApi.

    Signs with a local key so the signature MoonWalk gets back can be recovered,
    and keeps every request so the payload Circle would receive can be inspected.
    """

    def __init__(
        self,
        account: Any = CIRCLE,
        *,
        response: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.account = account
        self.response = response
        self.error = error
        self.requests: list[Any] = []

    def sign_typed_data(self, sign_typed_data_request: Any) -> Any:
        self.requests.append(sign_typed_data_request)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        typed = json.loads(sign_typed_data_request.data)
        signed = self.account.sign_message(encode_typed_data(full_message=typed))
        return dcw.SignatureResponse.from_dict(
            {"data": {"signature": "0x" + bytes(signed.signature).hex()}}
        )


def circle_signer(api: FakeSigningApi, *, address: str | None = None) -> wallets.CircleWalletSigner:
    ciphertexts = itertools.count(1)
    return wallets.CircleWalletSigner(
        api,
        wallet_id="62f9550a-59d8-54bc-a044-c7d97c442b78",
        address=address or CIRCLE.address,
        entity_secret_ciphertext=lambda: f"ciphertext-{next(ciphertexts)}",
    )


# ---- the EIP-712 payloads MoonWalk signs ---------------------------------


def test_usdc_domain_matches_the_live_separator() -> None:
    """Our EIP-712 domain has to be Arc USDC's, or every authorization is refused.

    The separator is rebuilt from the module's own domain field list and compared
    with the value the contract returns on chain.
    """
    typed = wallets.usdc_authorization_typed_data(
        "TransferWithAuthorization",
        sender=AGENT.address,
        recipient=CIRCLE.address,
        value=1,
        valid_after=0,
        valid_before=1,
        nonce=bytes(32),
        chain_id=ARC_CHAIN_ID,
        usdc_address=USDC,
    )
    signable = encode_typed_data(full_message=typed)
    assert signable.header == USDC_DOMAIN_SEPARATOR
    assert typed["domain"] == {
        "name": "USDC",
        "version": "2",
        "chainId": ARC_CHAIN_ID,
        "verifyingContract": USDC,
    }


@pytest.mark.parametrize(
    ("kind", "typehash"),
    [
        ("TransferWithAuthorization", TRANSFER_TYPEHASH),
        ("ReceiveWithAuthorization", RECEIVE_TYPEHASH),
    ],
)
def test_authorization_fields_hash_to_circles_typehash(kind: str, typehash: bytes) -> None:
    """Field names, order and types, checked against Circle's published constant."""
    assert keccak(text=type_string(kind, wallets.AUTHORIZATION_FIELDS)) == typehash


def test_jsonable_typed_data_keeps_the_digest() -> None:
    """Hexifying for Circle must not change what gets signed."""
    hexed = wallets.jsonable_typed_data(VOUCHER)
    assert hexed["message"]["channelId"] == "0x" + "11" * 32
    assert json.loads(json.dumps(hexed)) == hexed
    assert digest(hexed) == digest(VOUCHER)


def test_jsonable_typed_data_leaves_the_original_alone() -> None:
    before = VOUCHER["message"]["channelId"]
    wallets.jsonable_typed_data(VOUCHER)
    assert VOUCHER["message"]["channelId"] is before


# ---- the local backend ---------------------------------------------------


def test_local_signer_signs_and_recovers() -> None:
    signer = wallets.LocalSigner(AGENT)
    signature = signer.sign_typed_data(VOUCHER)
    assert signer.backend == "local"
    assert signer.address == AGENT.address
    assert len(signature) == 65
    assert wallets.recover_typed_data_signer(VOUCHER, signature) == AGENT.address


def test_local_signer_repr_holds_no_key() -> None:
    signer = wallets.LocalSigner.from_key("a1" * 32)
    text = repr(signer)
    assert signer.address == AGENT.address
    assert signer.account.address == AGENT.address
    assert signer.address in text
    assert "a1a1" not in text


def test_signers_satisfy_the_protocol() -> None:
    assert isinstance(wallets.LocalSigner(AGENT), wallets.Signer)
    assert isinstance(circle_signer(FakeSigningApi()), wallets.Signer)


# ---- the Circle backend --------------------------------------------------


def test_circle_signature_recovers_to_the_wallet() -> None:
    """The whole loop: build the payload, hand it to Circle, recover the signer."""
    signer = circle_signer(FakeSigningApi())
    signature = signer.sign_typed_data(VOUCHER)
    assert signer.backend == "circle-developer-controlled"
    assert len(signature) == 65
    assert wallets.recover_typed_data_signer(VOUCHER, signature) == CIRCLE.address


def test_circle_request_is_what_circle_expects() -> None:
    api = FakeSigningApi()
    signer = circle_signer(api)
    signer.sign_typed_data(VOUCHER)
    request = api.requests[0]
    assert request.wallet_id == "62f9550a-59d8-54bc-a044-c7d97c442b78"
    assert request.entity_secret_ciphertext == "ciphertext-1"
    # The payload is JSON, so the bytes32 fields have to arrive as hex.
    assert json.loads(request.data) == wallets.jsonable_typed_data(VOUCHER)


def test_every_signature_gets_a_fresh_ciphertext() -> None:
    """Circle mandates a unique entity secret ciphertext per request, so caching
    one would work in a demo and fail on the second voucher."""
    api = FakeSigningApi()
    signer = circle_signer(api)
    signer.sign_typed_data(VOUCHER)
    signer.sign_typed_data(VOUCHER)
    used = [request.entity_secret_ciphertext for request in api.requests]
    assert used == ["ciphertext-1", "ciphertext-2"]


def test_circle_wallet_signer_repr_has_no_secrets() -> None:
    signer = circle_signer(FakeSigningApi())
    text = repr(signer)
    assert signer.wallet_id in text
    assert CIRCLE.address in text
    assert "ciphertext" not in text


def test_circle_api_failure_becomes_a_signing_error() -> None:
    api = FakeSigningApi(error=dcw.ApiException(status=403, reason="Forbidden"))
    with pytest.raises(wallets.CircleSigningError) as caught:
        circle_signer(api).sign_typed_data(VOUCHER)
    assert "403" in str(caught.value)


@pytest.mark.parametrize(
    "signature",
    ["PENDING", "0x1234", ""],
)
def test_an_unusable_signature_is_rejected(signature: str) -> None:
    """A short or non-hex answer must fail here, not on chain."""
    api = FakeSigningApi(response=SimpleNamespace(data=SimpleNamespace(signature=signature)))
    with pytest.raises(wallets.CircleSigningError):
        circle_signer(api).sign_typed_data(VOUCHER)


def test_a_missing_signature_is_rejected() -> None:
    api = FakeSigningApi(response=SimpleNamespace(data=None))
    with pytest.raises(wallets.CircleSigningError):
        circle_signer(api).sign_typed_data(VOUCHER)


# ---- picking a backend ---------------------------------------------------


def test_signer_from_env_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOONWALK_SIGNER", raising=False)
    monkeypatch.setenv("AGENT_PRIVATE_KEY", "0x" + "a1" * 32)
    signer = wallets.signer_from_env()
    assert signer.backend == "local"
    assert signer.address == AGENT.address


def test_signer_from_env_rejects_an_unknown_backend() -> None:
    with pytest.raises(ValueError) as caught:
        wallets.signer_from_env("kms")
    assert "kms" in str(caught.value)


def test_local_backend_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PRIVATE_KEY", "")
    monkeypatch.setenv("DEPLOYER_PRIVATE_KEY", "")
    with pytest.raises(ValueError):
        wallets.signer_from_env("local")
