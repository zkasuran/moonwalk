"""HTML payment page generator.

Renders a self-contained page the user opens to pay for a premium command.
Shows command details, price, and a MetaMask EIP-3009 pay button.
"""

from __future__ import annotations

from ..chain import config as chain_config
from ..domain.models import PaymentRecord


def build_payment_page(
    record: PaymentRecord,
    requirements: dict,  # type: ignore[type-arg]
    base_url: str,
) -> str:
    price_usdc = record.price_atomic / 1_000_000
    cmd = record.command_name
    args_display = ", ".join(f"{k}={v}" for k, v in record.command_args.items())
    execute_url = f"{base_url}/execute/{record.payment_id}"
    status_url = f"{base_url}/status/{record.payment_id}"
    network_id = requirements.get("network", chain_config.ARC_NETWORK)
    asset = requirements.get("asset", "")
    pay_to = requirements.get("payTo", "")
    amount = requirements.get("maxAmountRequired", str(record.price_atomic))
    # The network the payer's wallet is told to add. All of it comes from config so
    # a mainnet page never says "testnet" and never quotes the wrong native
    # decimals: Arc's native gas balance is 18 decimals, not the 6 of the ERC-20.
    chain_name = chain_config.ARC_NETWORK_NAME
    rpc_url = chain_config.ARC_RPC_URL
    explorer_url = chain_config.ARC_EXPLORER
    native_decimals = chain_config.ARC_NATIVE_DECIMALS

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NanoPay: Pay to run /{cmd}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,sans-serif;background:#0f0f0f;color:#e0e0e0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
  .card{{background:#141414;border:1px solid #222;border-radius:16px;padding:32px;max-width:420px;width:100%}}
  h1{{font-size:1.2rem;color:#fff;margin-bottom:4px}}
  .sub{{font-size:0.85rem;color:#555;margin-bottom:24px}}
  .row{{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #1e1e1e}}
  .row:last-child{{border-bottom:none}}
  .key{{font-size:0.8rem;color:#666}}
  .val{{font-size:0.9rem;color:#ccc;text-align:right;word-break:break-all;max-width:260px}}
  .price{{font-size:1.5rem;font-weight:700;color:#4ade80;margin:20px 0;text-align:center}}
  .btn{{width:100%;padding:14px;background:#7c3aed;color:#fff;border:none;border-radius:10px;font-size:1rem;font-weight:600;cursor:pointer;margin-top:16px;transition:background 0.15s}}
  .btn:hover{{background:#6d28d9}}
  .btn:disabled{{background:#333;color:#666;cursor:not-allowed}}
  .status{{margin-top:16px;padding:12px;border-radius:8px;font-size:0.85rem;text-align:center;display:none}}
  .status.ok{{background:#1a3d2b;color:#4ade80;display:block}}
  .status.err{{background:#3d1a1a;color:#f87171;display:block}}
  .status.info{{background:#1a2a40;color:#60a5fa;display:block}}
  .mono{{font-family:monospace;font-size:0.8rem}}
  .arc-badge{{display:inline-flex;align-items:center;gap:6px;background:#1e1e2e;border:1px solid #2a2a4a;border-radius:6px;padding:4px 10px;font-size:0.75rem;color:#818cf8;margin-bottom:16px}}
</style>
</head>
<body>
<div class="card">
  <h1>NanoPay for Discord</h1>
  <p class="sub">Pay to run a premium command</p>

  <div class="arc-badge">
    <span>&#9679;</span> {chain_name} &nbsp;&#183;&nbsp; x402 EIP-3009
  </div>

  <div class="row">
    <span class="key">Command</span>
    <span class="val">/{cmd} {args_display}</span>
  </div>
  <div class="row">
    <span class="key">Payment ID</span>
    <span class="val mono">{record.payment_id[:16]}...</span>
  </div>
  <div class="row">
    <span class="key">Recipient</span>
    <span class="val mono">{pay_to[:10]}...{pay_to[-6:]}</span>
  </div>
  <div class="row">
    <span class="key">Token</span>
    <span class="val">USDC on Arc</span>
  </div>

  <div class="price">${price_usdc:.4f} USDC</div>

  <button class="btn" id="pay-btn" onclick="payWithMetaMask()">
    Pay with MetaMask
  </button>

  <div class="status" id="status-box"></div>

  <p style="font-size:0.72rem;color:#444;margin-top:16px;text-align:center">
    Powered by <strong>x402</strong> on {chain_name} (chain {network_id.split(":")[-1]})
  </p>
</div>

<script>
const NETWORK = "{network_id}";
const CHAIN_ID = {int(network_id.split(":")[-1])};
const CHAIN_NAME = "{chain_name}";
const RPC_URL = "{rpc_url}";
const EXPLORER_URL = "{explorer_url}";
const NATIVE_DECIMALS = {native_decimals};
const ASSET = "{asset}";
const PAY_TO = "{pay_to}";
const AMOUNT = "{amount}";
const EXECUTE_URL = "{execute_url}";
const STATUS_URL = "{status_url}";

function showStatus(msg, cls) {{
  const el = document.getElementById("status-box");
  el.className = "status " + cls;
  el.textContent = msg;
}}

async function switchToArc() {{
  try {{
    await ethereum.request({{
      method: "wallet_switchEthereumChain",
      params: [{{ chainId: "0x" + CHAIN_ID.toString(16) }}],
    }});
  }} catch (e) {{
    if (e.code === 4902) {{
      await ethereum.request({{
        method: "wallet_addEthereumChain",
        params: [{{
          chainId: "0x" + CHAIN_ID.toString(16),
          chainName: CHAIN_NAME,
          nativeCurrency: {{ name: "USDC", symbol: "USDC", decimals: NATIVE_DECIMALS }},
          rpcUrls: [RPC_URL],
          blockExplorerUrls: [EXPLORER_URL],
        }}],
      }});
    }}
  }}
}}

async function signEIP3009(from, validAfter, validBefore, nonce) {{
  const domain = {{
    name: "USDC",
    version: "2",
    chainId: CHAIN_ID,
    verifyingContract: ASSET,
  }};
  const types = {{
    TransferWithAuthorization: [
      {{ name: "from", type: "address" }},
      {{ name: "to", type: "address" }},
      {{ name: "value", type: "uint256" }},
      {{ name: "validAfter", type: "uint256" }},
      {{ name: "validBefore", type: "uint256" }},
      {{ name: "nonce", type: "bytes32" }},
    ],
  }};
  const message = {{
    from,
    to: PAY_TO,
    value: AMOUNT,
    validAfter,
    validBefore,
    nonce,
  }};
  return await ethereum.request({{
    method: "eth_signTypedData_v4",
    params: [from, JSON.stringify({{ domain, types, primaryType: "TransferWithAuthorization", message }})],
  }});
}}

function randomBytes32() {{
  const arr = new Uint8Array(32);
  crypto.getRandomValues(arr);
  return "0x" + Array.from(arr).map(b => b.toString(16).padStart(2, "0")).join("");
}}

async function payWithMetaMask() {{
  const btn = document.getElementById("pay-btn");
  if (!window.ethereum) {{
    showStatus("MetaMask not found. Install it to pay.", "err");
    return;
  }}
  btn.disabled = true;
  btn.textContent = "Connecting...";

  try {{
    const accounts = await ethereum.request({{ method: "eth_requestAccounts" }});
    const from = accounts[0];
    showStatus("Switching to " + CHAIN_NAME + "...", "info");
    await switchToArc();

    const now = Math.floor(Date.now() / 1000);
    const validAfter = 0;
    const validBefore = now + 300; // 5 min
    const nonce = randomBytes32();

    showStatus("Sign the payment in MetaMask...", "info");
    btn.textContent = "Waiting for signature...";
    const sig = await signEIP3009(from, validAfter, validBefore, nonce);

    const paymentPayload = {{
      x402Version: 1,
      scheme: "exact",
      network: NETWORK,
      payload: {{
        signature: sig,
        authorization: {{
          from,
          to: PAY_TO,
          value: AMOUNT,
          validAfter: validAfter.toString(),
          validBefore: validBefore.toString(),
          nonce,
        }},
      }},
    }};

    const encoded = btoa(JSON.stringify(paymentPayload));
    showStatus("Submitting payment...", "info");
    btn.textContent = "Submitting...";

    const resp = await fetch(EXECUTE_URL, {{
      method: "POST",
      headers: {{ "X-PAYMENT": encoded, "Content-Type": "application/json" }},
    }});
    const data = await resp.json();

    if (resp.ok && data.ok) {{
      showStatus("Paid! Tx: " + (data.tx_hash || "").slice(0, 20) + "... Check Discord.", "ok");
      btn.textContent = "Paid!";
      // Poll status for 30s to confirm bot follow-up
      pollStatus(10);
    }} else {{
      showStatus("Payment failed: " + (data.detail || data.error || resp.status), "err");
      btn.disabled = false;
      btn.textContent = "Retry";
    }}
  }} catch (e) {{
    showStatus("Error: " + (e.message || e), "err");
    btn.disabled = false;
    btn.textContent = "Pay with MetaMask";
  }}
}}

async function pollStatus(tries) {{
  if (tries <= 0) return;
  try {{
    const r = await fetch(STATUS_URL);
    const d = await r.json();
    if (d.status === "paid") {{
      showStatus("Command result delivered in Discord.", "ok");
      return;
    }}
  }} catch (_) {{}}
  setTimeout(() => pollStatus(tries - 1), 3000);
}}
</script>
</body>
</html>"""
