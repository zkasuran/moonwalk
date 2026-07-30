"""Circle's own products, wired into MoonWalk for real.

Two integrations live here, both exercised against Circle's live testnet APIs:

`wallets` gives the agent a second signer backend. The key can stay in this
process (`LocalSigner`) or live in Circle's developer-controlled wallet
infrastructure (`CircleWalletSigner`), where this repo never holds it.

`cctp` lets the agent refill its own Arc USDC balance across chains with CCTP V2:
burn on a source testnet, wait for Circle's Iris attestation, mint on Arc.

`gateway` is read-only on purpose. It reads Circle Gateway state so the
comparison with MoonWalk's own channel in docs/CIRCLE-INTEGRATIONS.md is written
against real numbers instead of prose. There is no Gateway payment rail here.

What is verified and what is not is spelled out in docs/CIRCLE-INTEGRATIONS.md.
"""

from __future__ import annotations

from .cctp import (
    CHAINS,
    Attestation,
    BuiltCall,
    CctpBridge,
    ChainConfig,
    FeeOption,
    FinalityThreshold,
    IrisClient,
    RefillPlan,
    RouteCheck,
    SentCall,
    chain,
    encode_call,
    selector,
)
from .gateway import GatewayBalance, GatewayOnchainState, GatewayReader
from .wallets import (
    CircleSigningError,
    CircleWalletSigner,
    LocalSigner,
    Signer,
    TypedData,
    jsonable_typed_data,
    recover_typed_data_signer,
    signer_from_env,
    usdc_authorization_typed_data,
)

__all__ = [
    "CHAINS",
    "Attestation",
    "BuiltCall",
    "CctpBridge",
    "ChainConfig",
    "CircleSigningError",
    "CircleWalletSigner",
    "FeeOption",
    "FinalityThreshold",
    "GatewayBalance",
    "GatewayOnchainState",
    "GatewayReader",
    "IrisClient",
    "LocalSigner",
    "RefillPlan",
    "RouteCheck",
    "SentCall",
    "Signer",
    "TypedData",
    "chain",
    "encode_call",
    "jsonable_typed_data",
    "recover_typed_data_signer",
    "selector",
    "signer_from_env",
    "usdc_authorization_typed_data",
]
