# moonwalk-nanopay

A Python SDK for the MoonWalk USDC payment channel on Arc mainnet. It wraps the
NanoChannel and SpendGuard contracts so you can open a channel, sign vouchers
off-chain, redeem them in batches then close.

The design splits who signs from who pays gas. The payer signs everything (the
deposit authorization, every voucher, the close agreement) and never sends a
transaction. Whoever wants the money on chain submits. On Arc they pay that gas
in the same USDC they are collecting. Every amount in this SDK is USDC atomic
units, the 6 decimal ERC-20 view.

Built on [web3.py](https://web3py.readthedocs.io) and
[eth-account](https://eth-account.readthedocs.io). Fully typed, `mypy --strict`
clean. MIT licensed.

## Install

```sh
pip install moonwalk-nanopay
```

Python 3.10 or newer. `web3` and `eth-account` come as dependencies.

## Quickstart

Read a live channel on Arc mainnet, then build and sign a voucher. Signing costs
the payer nothing: no gas, no transaction.

```python
from eth_account import Account
from moonwalk_nanopay import NanopayClient

client = NanopayClient()                       # Arc mainnet, public RPC
payer = Account.from_key("0x...")              # the payer's key
service = "0x00000000000000000000000000000000000000AA"
salt = bytes.fromhex("00" * 31 + "01")

channel_id = client.channel_id(payer.address, service, salt)
subject = client.subject_id("111111111111111111", "222222222222222222")

print("outstanding", client.outstanding(channel_id))
print("redeemed", client.subject_redeemed(channel_id, subject))

voucher = client.voucher(channel_id, subject, 250_000)   # 0.25 USDC cumulative
signature = client.sign_voucher(payer, voucher)          # gasless, off-chain
print("voucher signature", "0x" + signature.hex())
```

A runnable read-only version is in [`examples/read_live.py`](./examples/read_live.py).
It talks to the real chain:

```sh
python examples/read_live.py
```

## The two-signature open

Opening a channel needs two signatures from the payer, both built by
`open_channel`. The payer signs both then hands them to any submitter, so the
payer's wallet stays free of transactions.

1. An EIP-3009 `ReceiveWithAuthorization` on USDC. This moves the deposit. The
   `to` is fixed to the NanoChannel, so only the channel can pull the funds. It
   is signed under the USDC domain (name "USDC", version "2").
2. The `Open` struct under the NanoChannel domain (name "MoonWalk NanoChannel",
   version "1"). It binds every channel parameter (`guarded`, `cap_owner`, the
   opening cap) plus the deposit and the authorization nonce into one signature.
   Without it a submitter could open the channel with `guarded` off or point
   `cap_owner` at itself, so this second signature is what makes the caps hold.

```python
result = client.open_channel(
    payer=payer,
    service=service,
    salt=salt,
    guarded=True,
    deposit=5_000_000,       # 5 USDC
    cap_limit=1_000_000,     # 1 USDC default cap per subject
    cap_window=86_400,       # per day
)

# Hand result.to and result.data to any relayer or submit through a client
# constructed with a submitter account:
#   client = NanopayClient(submitter=relayer)
#   sent = result.submit()
print(result.channel_id.hex())
```

## API

`NanopayClient` is the entry point. Construct it with no arguments for Arc
mainnet over the public RPC. You can also pass an `rpc_url`, an `Addresses`
override, a `chain_id` or a `submitter` account for the party that sends
transactions.

Writes return a `PreparedTransaction` with `to`, `data` and `submit`. Use `to`
and `data` with your own relayer or call `submit()` to send through the client's
submitter account.

- `open_channel(...)`: build and sign both open signatures, return the ids and a ready transaction.
- `sign_voucher(payer, voucher)`: sign one cumulative total. Gasless.
- `voucher(channel_id, subject, cumulative, valid_before=None)`: construct a voucher with a validBefore.
- `redeem(channel_id, signed_vouchers)`: settle a batch in one transfer.
- `close_mutual(channel_id, payer_signature, service_signature)`: close immediately with both signatures.
- `request_close(channel_id)`: payer starts the challenge window. Sent by the payer.
- `withdraw(channel_id)`: payer takes the remainder once the window ends. Sent by the payer.
- `top_up(channel_id, authorization)`: add funds with another signed authorization.

Reads:

- `channel_of(channel_id)`, `outstanding(channel_id)`, `subject_redeemed(channel_id, subject)`, `challenge_window()`.
- SpendGuard `cap_of(channel_id, subject)`, `remaining(channel_id, subject)`, `usage_of(channel_id, subject)`.
- `usdc_balance(address)`.
- On-chain digests for cross-checks: `open_hash_onchain`, `close_hash_onchain`, `voucher_hash_onchain`, `domain_separator_onchain`.

Helpers, pure and usable without a client:

- `channel_id(payer, service, salt)`: `keccak256(abi.encode(payer, service, salt))`.
- `subject_id(guild_id, user_id)`: `keccak256("discord:<guild_id>:<user_id>")`.
- `voucher_digest`, `open_digest`, `close_digest`: the EIP-712 digest computed locally.
- `sign_open`, `sign_close`, `sign_receive_with_authorization`, `build_authorization`: the standalone signers.

## Amounts and decimals

Every amount here is USDC atomic units, the 6 decimal ERC-20 view. Gas on Arc is
paid in USDC through an 18 decimal native balance. This SDK never touches that 18
decimal view, because mixing the two is the classic Arc decimals bug.

## Addresses (Arc mainnet)

| Contract | Address |
| --- | --- |
| USDC | `0x3600000000000000000000000000000000000000` |
| NanoChannel | `0x059D3A87E91fA91D341f364868A9Ed333077989a` |
| SpendGuard | `0xAbB85ab157357676eBE7ae17A161168912A3c232` |
| ServiceRegistry | `0x1b9FF1FAD0181705B750C325B3A82137bB153866` |

Chain id 5042. RPC `https://rpc.mainnet.arc.io`. Explorer `https://explorer.arc.io`.

## Verifying against the deployed contract

The tests assert the SDK computes the same EIP-712 typehashes the contract stores
on chain (`OPEN_TYPEHASH` and `VOUCHER_TYPEHASH`). They also rebuild a voucher
digest and an open digest independently from the raw keccak256 pieces of the
EIP-712 spec and assert the SDK matches. A match means every signature the SDK
produces will verify against the deployed NanoChannel.

```sh
pytest -q
```

## License

MIT. See [LICENSE](./LICENSE).
