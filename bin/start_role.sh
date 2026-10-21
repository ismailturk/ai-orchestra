#!/bin/bash
# Interaktif rol oturumu baslatir (cmux paneli icin).
# Rol promptu team.py launch --dry-run'dan gelir: tek kaynak.
# Kullanim: start_role.sh <proje-yolu> <rol> [TASK-ID]
set -uo pipefail

PROJECT="${1:?proje yolu gerekli}"
ROLE="${2:?rol gerekli}"
TASK="${3:-}"
TEAM_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/team.py"

ARGS=(--project "$PROJECT" launch "$ROLE" --dry-run)
[[ -n "$TASK" ]] && ARGS=(--project "$PROJECT" launch "$ROLE" --task "$TASK" --dry-run)

PROMPT="$(python3 "$TEAM_PY" "${ARGS[@]}" 2>/dev/null | python3 -c 'import shlex,sys
try: print(shlex.split(sys.stdin.read())[-1])
except Exception: pass')"

if [[ -z "$PROMPT" ]]; then
  echo "STOP: '$ROLE' icin rol promptu uretilemedi." >&2
  exit 3
fi

case "$ROLE" in
  analyst|designer|verifier)
    echo "▶ $ROLE · Codex baslatiliyor (sandbox: workspace-write)"
    exec codex -C "$PROJECT" -s workspace-write "$PROMPT"
    ;;
  lead|developer|tester)
    echo "▶ $ROLE · Claude Code baslatiliyor"
    cd "$PROJECT" || exit 1
    exec claude "$PROMPT"
    ;;
  *)
    echo "STOP: bilinmeyen rol '$ROLE'" >&2; exit 2 ;;
esac
