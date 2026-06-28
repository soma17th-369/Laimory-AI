# .agents/skills 아래 각 스킬을 .claude/skills 에 정션(junction)으로 연결합니다.
# 사용법(레포 루트에서): pwsh scripts/link-skills.ps1
# Codex 는 .agents/skills 를 직접 인식하고, Claude 는 여기서 만든 .claude/skills 정션을 통해 같은 원본을 봅니다.

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent $PSScriptRoot
$agentsRoot = Join-Path $repoRoot '.agents/skills'
$claudeRoot = Join-Path $repoRoot '.claude/skills'

if (-not (Test-Path $agentsRoot)) {
    Write-Error ".agents/skills 가 없습니다: $agentsRoot"
}

New-Item -ItemType Directory -Force -Path $claudeRoot | Out-Null

Get-ChildItem -Path $agentsRoot -Directory | ForEach-Object {
    $name   = $_.Name
    $target = $_.FullName
    $link   = Join-Path $claudeRoot $name

    # 기존 항목 정리: 정션/일반파일/폴더 무엇이든 제거 후 다시 연결
    if (Test-Path $link) {
        Remove-Item $link -Recurse -Force
    }

    New-Item -ItemType Junction -Path $link -Target $target | Out-Null
    Write-Host "linked  .claude/skills/$name  ->  .agents/skills/$name"
}

Write-Host "done."
