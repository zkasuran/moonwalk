"""Two ways for the agent to hold a key, one interface.

MoonWalk's rails only ever ask a wallet for one thing: an EIP-712 signature. The
channel voucher, the close agreement and the EIP-3009 authorization that funds a
channel are all typed data, so a signer needs an address and a way to sign a
typed payload. That is the whole `Signer` protocol below.

`LocalSigner` is what the demos and the tests use. The key sits in this process,
read from .env. Fast and free, and the operator carries the risk of that file.

`CircleWalletSigner` puts the key in Circle's developer-controlled wallet instead.
This process never sees a private key: signing is an API call, Circle signs inside
its own custody and returns the 65 bytes. That is the honest production answer for
an agent that spends real money. Access is an API credential you can rotate, and a
leaked clone of this repo does not leak the wallet.

Verified live on 2026-07-29: Circle signed a MoonWalk voucher payload for Arc
testnet (chain 5042002) and the recovered signer matched the wallet address.
Receipts and the parts that are not verified are in docs/CIRCLE-INTEGRATIONS.md.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from web3 import Web3

from src.chain import config as chain_config

# An EIP-712 payload in eth_account's "full message" shape: types, primaryType,
# domain, message. Values are deliberately loose because the message half is
# whatever the struct says.
TypedData = dict[str, Any]

AuthorizationKind = Literal["TransferWithAuthorization", "ReceiveWithAuthorization"]

EIP712_DOMAIN = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

# EIP-3009. Both authorization structs carry the same fields, which is why the
# typehashes differ only by the struct name.
AUTHORIZATION_FIELDS = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


class CircleSigningError(RuntimeError):
    """Circle would not sign, or answered with something we cannot use."""


def jsonable_typed_data(typed_data: TypedData) -> TypedData:
    """The same payload with every `bytes` value turned into a hex string.

    This matters for the Circle backend and not at all for the local one. Locally
    eth_account takes raw bytes for a bytes32 field, which is how src/chain builds
    its payloads. Circle's API takes the payload as a JSON string, and bytes are
    not JSON. Hexifying here means a payload built for the local signer can be
    handed to the Circle signer unchanged, and the digest is identical either way.
    """

    def convert(value: Any) -> Any:
        if isinstance(value, bytes):
            return "0x" + value.hex()
        if isinstance(value, dict):
            return {str(k): convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    converted: TypedData = {str(k): convert(v) for k, v in typed_data.items()}
    return converted


def recover_typed_data_signer(typed_data: TypedData, signature: bytes | str) -> str:
    """Which address produced this EIP-712 signature.

    Used to check a Circle signature locally instead of taking the API's word for
    which wallet signed. A backend that returns a signature for the wrong key
    fails here rather than on chain.
    """
    message = encode_typed_data(full_message=typed_data)
    return str(Account.recover_message(message, signature=signature))


def usdc_authorization_typed_data(
    kind: AuthorizationKind,
    *,
    sender: str,
    recipient: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes | str,
    chain_id: int | None = None,
    usdc_address: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> TypedData:
    """The EIP-3009 payload Arc USDC verifies, ready for either backend.

    `TransferWithAuthorization` lets any relayer move the funds to `recipient`.
    `ReceiveWithAuthorization` additionally requires `to == msg.sender`, which is
    what the channel uses so only the channel contract can pull a deposit.

    The domain is Arc USDC's: name "USDC", version "2", chainId 5042002,
    verifyingContract 0x3600...0000. That was confirmed by recomputing the
    separator and matching the live DOMAIN_SEPARATOR() on chain.
    """
    return {
        "types": {"EIP712Domain": EIP712_DOMAIN, kind: AUTHORIZATION_FIELDS},
        "primaryType": kind,
        "domain": {
            "name": name or chain_config.USDC_DOMAIN_NAME,
            "version": version or chain_config.USDC_DOMAIN_VERSION,
            "chainId": chain_id if chain_id is not None else chain_config.ARC_CHAIN_ID,
            "verifyingContract": Web3.to_checksum_address(
                usdc_address or chain_config.USDC_ADDRESS
            ),
        },
        "message": {
            "from": Web3.to_checksum_address(sender),
            "to": Web3.to_checksum_address(recipient),
            "value": value,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce if isinstance(nonce, str) else "0x" + nonce.hex(),
        },
    }


@runtime_checkable
class Signer(Protocol):
    """An address and a way to sign EIP-712 typed data. Nothing else.

    Deliberately narrow. Everything MoonWalk signs is typed data, so a backend
    that can do this one thing can drive the whole payment rail, and the rest of
    the app never learns where the key lives.
    """

    @property
    def backend(self) -> str: ...

    @property
    def address(self) -> str: ...

    def sign_typed_data(self, typed_data: TypedData) -> bytes: ...


class LocalSigner:
    """The key is in this process. The default, and the only one the tests use."""

    def __init__(self, account: LocalAccount) -> None:
        self._account = account

    @property
    def backend(self) -> str:
        return "local"

    @property
    def address(self) -> str:
        return str(self._account.address)

    @property
    def account(self) -> LocalAccount:
        """The underlying eth_account signer, for the paths that still want one
        (submitting a transaction needs a key here, not a remote signature)."""
        return self._account

    def sign_typed_data(self, typed_data: TypedData) -> bytes:
        signed = self._account.sign_message(encode_typed_data(full_message=typed_data))
        return bytes(signed.signature)

    @classmethod
    def from_key(cls, private_key: str) -> LocalSigner:
        key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        return cls(Account.from_key(key))

    def __repr__(self) -> str:
        # Never let a key reach a log line through a repr.
        return f"LocalSigner(address={self.address})"


class TypedDataSigningApi(Protocol):
    """The one Circle SDK call this module makes.

    Narrowed to a protocol so a test can stand in for Circle's client without a
    network, and so the SDK's generated surface stays out of the rest of the app.
    """

    def sign_typed_data(self, sign_typed_data_request: Any) -> Any: ...


class CircleWalletSigner:
    """The key lives in Circle's infrastructure. This process never holds it.

    Signing is `POST /v1/w3s/developer/sign/typedData`: MoonWalk sends the EIP-712
    payload as JSON, Circle signs it with the wallet's key inside its own custody
    and returns 65 bytes. Circle requires the entity secret to be re-encrypted for
    every request, which is why the ciphertext arrives as a factory rather than a
    value.
    """

    def __init__(
        self,
        api: TypedDataSigningApi,
        *,
        wallet_id: str,
        address: str,
        entity_secret_ciphertext: Callable[[], str],
        memo: str = "MoonWalk EIP-712",
    ) -> None:
        self._api = api
        self._wallet_id = wallet_id
        self._address = Web3.to_checksum_address(address)
        self._ciphertext = entity_secret_ciphertext
        self._memo = memo

    @property
    def backend(self) -> str:
        return "circle-developer-controlled"

    @property
    def address(self) -> str:
        return str(self._address)

    @property
    def wallet_id(self) -> str:
        return self._wallet_id

    def sign_typed_data(self, typed_data: TypedData) -> bytes:
        from circle.web3 import developer_controlled_wallets as dcw

        payload = json.dumps(jsonable_typed_data(typed_data), separators=(",", ":"))
        request = dcw.SignTypedDataRequest.from_dict(
            {
                "walletId": self._wallet_id,
                "data": payload,
                "memo": self._memo,
                "entitySecretCiphertext": self._ciphertext(),
            }
        )
        try:
            response = self._api.sign_typed_data(sign_typed_data_request=request)
        except dcw.ApiException as exc:
            raise CircleSigningError(f"Circle refused to sign: {exc.status} {exc.reason}") from exc
        signature = getattr(getattr(response, "data", None), "signature", None)
        if not isinstance(signature, str) or not signature.startswith("0x"):
            raise CircleSigningError(f"Circle returned no usable signature: {response!r}")
        raw = bytes.fromhex(signature[2:])
        if len(raw) != 65:
            raise CircleSigningError(f"expected a 65 byte signature, got {len(raw)}")
        return raw

    @classmethod
    def from_env(cls, *, memo: str = "MoonWalk EIP-712") -> CircleWalletSigner:
        """Build from the CIRCLE_* variables.

        This talks to Circle on construction: the SDK fetches the entity public key
        so the entity secret can be encrypted. That is why it is not test safe by
        design, and why the tests inject an api object instead.
        """
        from circle.web3 import developer_controlled_wallets as dcw

        # circle.web3.utils ships without a py.typed marker, unlike the generated
        # api package next to it, so mypy cannot see through it.
        from circle.web3 import utils as circle_utils  # type: ignore[attr-defined]

        missing = [
            name
            for name in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET", "CIRCLE_WALLET_ID")
            if not os.getenv(name)
        ]
        if missing:
            raise CircleSigningError(f"missing environment: {', '.join(missing)}")
        api_key = os.environ["CIRCLE_API_KEY"]
        entity_secret = os.environ["CIRCLE_ENTITY_SECRET"]
        wallet_id = os.environ["CIRCLE_WALLET_ID"]
        client = circle_utils.init_developer_controlled_wallets_client(
            api_key=api_key, entity_secret=entity_secret
        )
        address = os.getenv("CIRCLE_WALLET_ADDRESS", "")
        if not address:
            wallet = dcw.WalletsApi(client).get_wallet(id=wallet_id)  # type: ignore[no-untyped-call]
            address = str(wallet.data.wallet.address)
        return cls(
            dcw.SigningApi(client),  # type: ignore[no-untyped-call]
            wallet_id=wallet_id,
            address=address,
            # Circle mandates a fresh ciphertext per request, so this is called
            # once per signature and never cached.
            entity_secret_ciphertext=lambda: str(
                circle_utils.generate_entity_secret_ciphertext(api_key, entity_secret)
            ),
            memo=memo,
        )

    def __repr__(self) -> str:
        # No api key, no entity secret, no ciphertext. Only public identifiers.
        return f"CircleWalletSigner(wallet_id={self._wallet_id}, address={self.address})"


def signer_from_env(backend: str | None = None) -> Signer:
    """Pick a signer backend.

    `MOONWALK_SIGNER=circle` uses the Circle developer-controlled wallet, anything
    else uses AGENT_PRIVATE_KEY locally. Local is the default because the rest of
    MoonWalk still submits transactions from a local key and because Circle
    signing costs a network round trip per voucher, which a metered rail feels.
    The tradeoff is written up in docs/CIRCLE-INTEGRATIONS.md.
    """
    choice = backend if backend else os.environ.get("MOONWALK_SIGNER", "local")
    choice = choice.strip().lower()

    if choice in {"circle", "circle-developer-controlled"}:
        return CircleWalletSigner.from_env()
    if choice not in {"local", ""}:
        raise ValueError(f"unknown signer backend {choice!r}, expected 'local' or 'circle'")
    key = os.getenv("AGENT_PRIVATE_KEY", "") or os.getenv("DEPLOYER_PRIVATE_KEY", "")
    if not key:
        raise ValueError("local signer needs AGENT_PRIVATE_KEY (or DEPLOYER_PRIVATE_KEY)")
    return LocalSigner.from_key(key)
