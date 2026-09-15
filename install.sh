#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' "Erreur : Python 3 est requis. Installez-le depuis https://www.python.org/downloads/ puis relancez ./install.sh." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' "Création de l'environnement Python local…"
  python3 -m venv .venv
fi

printf '%s\n' "Installation des dépendances…"
.venv/bin/python -m pip install -r requirements.txt
printf '\n%s\n' "Installation terminée. Lancez ./export.sh pour commencer."
