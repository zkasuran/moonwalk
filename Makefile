.PHONY: install dev-install lint test test-all bot api contracts-build contracts-test abis deploy-arc channel-demo

install:
	uv venv && uv pip install -e .

dev-install:
	uv venv && uv pip install -e ".[dev]"

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/
	uv run mypy src/

test:
	uv run pytest -q

bot:
	uv run python run_bot.py

api:
	uv run python run_api.py

api-dev:
	uv run uvicorn src.api.app:app --host 0.0.0.0 --port 8402 --reload

fmt:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

# ---- contracts (Foundry) -------------------------------------------------

contracts-build:
	cd contracts && forge build

contracts-test:
	cd contracts && forge test

# Regenerate the committed ABIs from a fresh build. The runtime reads these, not
# the forge output, so the app never needs a toolchain.
abis: contracts-build
	@for n in NanoChannel SpendGuard ServiceRegistry; do \
		jq '.abi' contracts/out/$$n.sol/$$n.json > src/chain/abis/$$n.json; \
		echo "abi $$n"; \
	done

deploy-arc:
	cd contracts && forge script script/Deploy.s.sol:Deploy \
		--rpc-url $${ARC_RPC_URL:-https://rpc.testnet.arc.network} --broadcast --slow

# Live proof on Arc testnet: open, meter, batch redeem, refuse an over-cap
# voucher, close. Writes evidence/channel-<timestamp>.json.
channel-demo:
	uv run python scripts/channel_demo.py

test-all: test contracts-test
