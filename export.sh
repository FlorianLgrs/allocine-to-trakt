#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' "L'environnement Python n'existe pas encore. Lancez d'abord ./install.sh." >&2
  exit 1
fi

exec .venv/bin/python allocine_to_trakt.py "$@"
