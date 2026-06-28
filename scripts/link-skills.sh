#!/usr/bin/env bash
# .agents/skills 아래 각 스킬을 .claude/skills 에 복사합니다. (mac/linux)
# 사용법(레포 루트에서): bash scripts/link-skills.sh
# Codex 는 .agents/skills 를 직접 인식하고, Claude 는 .claude/skills 복사본을 봅니다.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
agents_root="$repo_root/.agents/skills"
claude_root="$repo_root/.claude/skills"

if [ ! -d "$agents_root" ]; then
    echo ".agents/skills 가 없습니다: $agents_root" >&2
    exit 1
fi

mkdir -p "$claude_root"

for target in "$agents_root"/*/; do
    [ -d "$target" ] || continue
    name="$(basename "$target")"
    copy="$claude_root/$name"

    # 기존 항목 정리 후 복사
    rm -rf "$copy"
    cp -R "$target" "$copy"
    echo "copied  .agents/skills/$name  ->  .claude/skills/$name"
done

echo "done."
