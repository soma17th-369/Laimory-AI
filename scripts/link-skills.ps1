# .agents/skills 아래 각 스킬을 .claude/skills 에 복사합니다.
# 사용법(레포 루트에서): pwsh scripts/link-skills.ps1
# Codex 는 .agents/skills 를 직접 인식하고, Claude 는 .claude/skills 복사본을 봅니다.

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
    $copy   = Join-Path $claudeRoot $name

    # 기존 항목 정리: 정션/일반파일/폴더 무엇이든 제거 후 다시 복사
    if (Test-Path $copy) {
        Remove-Item $copy -Recurse -Force
    }

    Copy-Item -Path $target -Destination $copy -Recurse -Force
    Write-Host "copied  .agents/skills/$name  ->  .claude/skills/$name"
}

Write-Host "done."
