#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export API_PORT="${API_PORT:-8000}" ANVIL_PORT="${ANVIL_PORT:-8545}"
export REGISTRY_ADDRESS="0x5FbDB2315678afecb367f032d93F642f64180aa3"

help() {
  cat <<'USAGE'
Usage: ./do <command>

  test       unit tests (no Docker) and Foundry tests
  check      ruff, mypy, forge fmt, lockfile, requirement exports, contract artifact
  fmt        format Python and Solidity
  certs      issue the parent's short-lived mTLS identity into ./certs
  pcr0       print the enclave PCR0 this checkout should attest to
  dev        build and start the stack, then fetch the mock NSM's root certificate
  demo       run two tenants' contracts through a running stack
  smoke      fresh stack, end-to-end and privacy checks, teardown (replaces a running stack)
  down       stop the stack and delete its volumes
  key NAME   create tenant NAME on the running stack and print a new API key
  lock       relock dependencies and re-export the hashed image requirements
  contracts  build the contract and refresh its ABI/bytecode artifact

API_PORT (default 8000) and ANVIL_PORT (default 8545) set the published ports.
USAGE
}

requirements() {
  local groups=(--no-default-groups)
  [[ $1 == parent ]] && groups+=(--group parent)
  uv export --frozen "${groups[@]}" --no-emit-project --no-header --format requirements-txt -q
}

pcr0() { uv run --quiet python -m nsm.measure; }

certs() { uv run --quiet python -m scripts.certs certs "$@"; }

dev() {
  certs --if-stale
  EXPECTED_PCR0="$(pcr0)"
  export EXPECTED_PCR0
  docker compose up --build --detach --wait --quiet-pull
  mkdir -p trust
  docker compose cp nsm:/trust/nsm-root.pem trust/nsm-root.pem
  echo "API http://localhost:$API_PORT  anvil http://localhost:$ANVIL_PORT"
  echo "enclave PCR0 $EXPECTED_PCR0"
}

key() { docker compose exec -T api python -m parent.admin create-tenant "$1"; }

client_env() {
  export API_URL="http://localhost:$API_PORT" ETH_RPC_URL="http://localhost:$ANVIL_PORT"
  export NSM_TRUST_ANCHOR="$PWD/trust/nsm-root.pem" EXPECTED_PCR0="${EXPECTED_PCR0:-$(pcr0)}"
}

smoke() {
  trap 'docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true' EXIT
  docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  dev
  client_env
  export SMOKE_ACME_KEY SMOKE_GLOBEX_KEY
  SMOKE_ACME_KEY="$(key acme)"
  SMOKE_GLOBEX_KEY="$(key globex)"
  if ! uv run pytest -m smoke tests/smoke -v; then
    docker compose logs --no-color --tail 40
    return 1
  fi
}

case "${1:-help}" in
  test)
    uv run pytest "${@:2}"
    forge test
    ;;
  check)
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    forge fmt --check
    uv lock --check
    diff -u enclave/requirements.txt <(requirements enclave)
    diff -u parent/requirements.txt <(requirements parent)
    forge build --quiet
    uv run python -m scripts.contract_artifact --check
    ;;
  fmt)
    uv run ruff format .
    uv run ruff check --fix .
    forge fmt
    ;;
  certs) certs ;;
  pcr0) pcr0 ;;
  dev) dev ;;
  demo)
    client_env
    DEMO_KEYS="acme=$(key acme),globex=$(key globex)" uv run python -m scripts.demo
    ;;
  smoke) smoke ;;
  down) docker compose down --volumes --remove-orphans ;;
  key) key "${2:?usage: ./do key NAME}" ;;
  lock)
    uv lock
    requirements enclave > enclave/requirements.txt
    requirements parent > parent/requirements.txt
    ;;
  contracts)
    forge build
    uv run python -m scripts.contract_artifact
    ;;
  help | -h | --help) help ;;
  *)
    help >&2
    exit 1
    ;;
esac
