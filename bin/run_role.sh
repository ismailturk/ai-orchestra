#!/bin/bash
# AI-Team headless Codex role runner.
# Rol promptunu team.py launch --dry-run'dan alir (tek kaynak), codex exec ile calistirir.
# Kullanim: run_role.sh <proje-yolu> <analyst|designer|verifier> [TASK-ID]
set -euo pipefail

PROJECT="${1:?proje yolu gerekli}"
ROLE="${2:?rol gerekli}"
TASK="${3:-}"
TEAM_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/team.py"

case "$ROLE" in
  analyst|designer|verifier) ;;
  *) echo "HATA: headless calistirilabilir roller: analyst, designer, verifier" >&2; exit 2 ;;
esac

ARGS=(--project "$PROJECT" launch "$ROLE" --dry-run)
[[ -n "$TASK" ]] && ARGS=(--project "$PROJECT" launch "$ROLE" --task "$TASK" --dry-run)

PROMPT="$(python3 "$TEAM_PY" "${ARGS[@]}" | python3 -c 'import shlex,sys; print(shlex.split(sys.stdin.read())[-1])')"

if [[ -z "$PROMPT" ]]; then
  echo "HATA: rol promptu uretilemedi" >&2; exit 3
fi

exec codex exec --cd "$PROJECT" -s workspace-write "$PROMPT"
