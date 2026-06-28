#!/usr/bin/env bash
# .agents/skills 아래 각 스킬을 .claude/skills 에 심볼릭 링크로 연결합니다. (mac/linux)
# 사용법(레포 루트에서): bash scripts/link-skills.sh
# Codex 는 .agents/skills 를 직접 인식하고, Claude 는 여기서 만든 .claude/skills 링크를 통해 같은 원본을 봅니다.

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
    link="$claude_root/$name"

    # 기존 항목 정리 후 상대경로 심볼릭 링크 생성
    rm -rf "$link"
    ln -s "../../.agents/skills/$name" "$link"
    echo "linked  .claude/skills/$name  ->  .agents/skills/$name"
done

echo "done."
