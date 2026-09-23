"""Offline client tests: calldata builders and their decoding, no network.

The client builds ``open()``, ``redeem()`` and the close calldata without a node.
These decode the calldata back and check the SDK encodes the arguments the
contract expects, the same way the TypeScript SDK's client test does.
"""

from __future__ import annotations

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from web3 import Web3

from moonwalk_nanopay import NanopayClient, SignedVoucher, Voucher, channel_id, load_abi

_TEST_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
_SERVICE = "0x00000000000000000000000000000000000000AA"
_SALT = bytes.fromhex("33" * 32)
_SUBJECT = bytes.fromhex("22" * 32)

_PAYER: LocalAccount = Account.from_key(_TEST_KEY)
_CLIENT = NanopayClient()
_DECODER = Web3().eth.contract(abi=load_abi("NanoChannel"))


def _decode(data: str) -> tuple[str, dict[str, object]]:
    fn, args = _DECODER.decode_function_input(HexStr(data))
    return fn.fn_name, args


def test_open_channel_signs_both_and_encodes_open() -> None:
    result = _CLIENT.open_channel(
        payer=_PAYER,
        service=_SERVICE,
        salt=_SALT,
        guarded=True,
        deposit=5_000_000,
        cap_limit=1_000_000,
        cap_window=86_400,
        valid_before=1_800_000_000,
        nonce=bytes.fromhex("44" * 32),
    )
    assert result.to.lower() == _CLIENT.addresses.nano_channel.lower()
    assert result.channel_id == channel_id(_PAYER.address, _SERVICE, _SALT)
    assert result.authorization.payer == _PAYER.address
    assert result.authorization.value == 5_000_000
    assert len(result.open_signature) == 65

    name, args = _decode(result.data)
    assert name == "open"
    assert args["guarded"] is True
    assert args["capLimit"] == 1_000_000
    auth = args["auth"]
    assert isinstance(auth, dict)
    assert auth["from"] == _PAYER.address
    assert auth["value"] == 5_000_000


def test_redeem_encodes_vouchers_and_signatures() -> None:
    cid = channel_id(_PAYER.address, _SERVICE, _SALT)
    voucher = _CLIENT.voucher(cid, _SUBJECT, 2_000_000, valid_before=1_800_000_000)
    signature = _CLIENT.sign_voucher(_PAYER, voucher)
    tx = _CLIENT.redeem(cid, [SignedVoucher(voucher=voucher, signature=signature)])
    name, args = _decode(tx.data)
    assert name == "redeem"
    assert args["channelId"] == cid
    vouchers = args["vouchers"]
    assert isinstance(vouchers, list)
    assert len(vouchers) == 1
    assert vouchers[0]["cumulative"] == 2_000_000
    signatures = args["signatures"]
    assert isinstance(signatures, list)
    assert signatures[0] == signature


def test_close_request_and_withdraw_selectors() -> None:
    cid = channel_id(_PAYER.address, _SERVICE, _SALT)
    assert _decode(_CLIENT.request_close(cid).data)[0] == "requestClose"
    assert _decode(_CLIENT.withdraw(cid).data)[0] == "withdraw"
    assert _decode(_CLIENT.close_mutual(cid, b"\xaa" * 65, b"\xbb" * 65).data)[0] == "closeMutual"


def test_submit_without_submitter_raises() -> None:
    cid = channel_id(_PAYER.address, _SERVICE, _SALT)
    with pytest.raises(RuntimeError, match="no submitter"):
        _CLIENT.request_close(cid).submit()


def test_signed_voucher_wire_round_trip() -> None:
    cid = channel_id(_PAYER.address, _SERVICE, _SALT)
    voucher = Voucher(channel_id=cid, subject=_SUBJECT, cumulative=42, valid_before=1_800_000_000)
    signed = SignedVoucher(voucher=voucher, signature=_CLIENT.sign_voucher(_PAYER, voucher))
    restored = SignedVoucher.from_wire(signed.to_wire())
    assert restored == signed
    assert _CLIENT.recover_voucher(restored.voucher, restored.signature) == _PAYER.address
