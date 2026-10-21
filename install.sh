#!/usr/bin/env bash
# AI-Team kurulumu (idempotent). Kullanim: ./install.sh [--claude-rules]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
RULES=0
for a in "$@"; do case "$a" in --claude-rules) RULES=1;; *) echo "Bilinmeyen arguman: $a" >&2; exit 2;; esac; done

command -v python3 >/dev/null || { echo "HATA: python3 yok" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' || { echo "HATA: python3 >= 3.10 gerekir" >&2; exit 1; }

mkdir -p "$BIN_DIR"
wrap() { printf '#!/bin/sh\nexec python3 "%s" "$@"\n' "$2" > "$BIN_DIR/$1"; chmod +x "$BIN_DIR/$1"; echo "ok  $BIN_DIR/$1"; }
wrap team "$DIR/bin/team.py"
wrap team-runner "$DIR/bin/team_runner.py"

case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "UYARI: $BIN_DIR PATH'te degil. ~/.zshrc'ye ekle: export PATH=\"\$HOME/.local/bin:\$PATH\"";; esac
for t in claude codex git; do command -v "$t" >/dev/null && echo "ok  $t" || echo "UYARI: $t bulunamadi"; done

# Claude becerileri: ~/.claude/skills/<ad>/ (farkli icerik varsa .bak yedegi alinir)
for sd in "$DIR"/skills/*/; do
  [ -d "$sd" ] || continue
  n="$(basename "$sd")"; T="$HOME/.claude/skills/$n"; mkdir -p "$T"
  for f in "$sd"*; do
    b="$(basename "$f")"
    if [ -f "$T/$b" ] && ! cmp -s "$f" "$T/$b"; then cp "$T/$b" "$T/$b.bak"; echo "UYARI: $T/$b degisti, yedek: $b.bak"; fi
    cp "$f" "$T/$b"
  done
  echo "ok  skill $n -> $T"
done

if [ "$RULES" = 1 ]; then
  F="$HOME/.claude/CLAUDE.md"; mkdir -p "$HOME/.claude"
  if [ -f "$F" ] && grep -q 'ai-team-core' "$F"; then echo "ok  $F zaten isaretli"
  else
    { [ -f "$F" ] && echo; sed "s#@AI_TEAM_DIR@#$DIR#g" "$DIR/templates/CLAUDE-global.md"; } >> "$F"
    echo "ok  cekirdek kurallar $F sonuna eklendi"
  fi
fi
echo "Bitti. Kontrol: team doctor"
