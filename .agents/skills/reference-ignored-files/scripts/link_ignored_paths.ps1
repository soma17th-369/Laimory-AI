[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string[]]$TargetPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-GitChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    $output = & git @ArgumentList 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($ArgumentList -join ' ')"
    }

    return ($output | Out-String).Trim()
}

function Test-PathInsideRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Candidate,
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $rootPrefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return $Candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
}

$worktreeRoot = [IO.Path]::GetFullPath(
    (Invoke-GitChecked -ArgumentList @('rev-parse', '--show-toplevel'))
)
$commonGitDirectory = [IO.Path]::GetFullPath(
    (Invoke-GitChecked -ArgumentList @('rev-parse', '--path-format=absolute', '--git-common-dir'))
)

if ((Split-Path -Leaf $commonGitDirectory) -ne '.git') {
    throw "The Git common directory does not belong to a regular worktree: $commonGitDirectory"
}

$mainWorktreeRoot = [IO.Path]::GetFullPath((Split-Path -Parent $commonGitDirectory))
$worktreePrefix = $worktreeRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$seenTargets = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$operations = [Collections.Generic.List[object]]::new()
$skipped = [Collections.Generic.List[object]]::new()

foreach ($rawTarget in $TargetPath) {
    if ([string]::IsNullOrWhiteSpace($rawTarget)) {
        throw 'An empty target path is not allowed.'
    }
    if ([IO.Path]::IsPathRooted($rawTarget)) {
        throw "Only repository-relative paths are allowed: $rawTarget"
    }

    $destination = [IO.Path]::GetFullPath((Join-Path $worktreeRoot $rawTarget))
    if (-not (Test-PathInsideRoot -Candidate $destination -Root $worktreeRoot)) {
        throw "A target cannot escape the repository: $rawTarget"
    }

    $relativePath = $destination.Substring($worktreePrefix.Length)
    if (-not $seenTargets.Add($relativePath)) {
        continue
    }

    if (Test-Path -LiteralPath $destination) {
        $skipped.Add([pscustomobject]@{
            RelativePath = $relativePath
            Destination = $destination
        })
        continue
    }

    $source = [IO.Path]::GetFullPath((Join-Path $mainWorktreeRoot $relativePath))
    if (-not (Test-PathInsideRoot -Candidate $source -Root $mainWorktreeRoot)) {
        throw "The source path escapes the main worktree: $relativePath"
    }
    if (-not (Test-Path -LiteralPath $source)) {
        throw "The source does not exist in the main worktree: $relativePath"
    }

    $sourceItem = Get-Item -Force -LiteralPath $source
    $ignoreProbe = if ($sourceItem.PSIsContainer) {
        $relativePath.TrimEnd('\', '/') + '/'
    } else {
        $relativePath
    }

    & git -C $worktreeRoot check-ignore -q -- $ignoreProbe
    $ignoreExitCode = $LASTEXITCODE
    if ($ignoreExitCode -eq 1) {
        throw "The target is not ignored by Git: $relativePath"
    }
    if ($ignoreExitCode -ne 0) {
        throw "Failed to check the Git ignore rule: $relativePath"
    }

    $parentDirectory = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
        throw "The destination parent is missing; link its parent directory instead: $relativePath"
    }

    $linkType = if ($sourceItem.PSIsContainer) { 'Junction' } else { 'HardLink' }
    if ($linkType -eq 'HardLink' -and
        [IO.Path]::GetPathRoot($source) -ne [IO.Path]::GetPathRoot($destination)) {
        throw "A hard link requires source and destination on the same volume: $relativePath"
    }

    $operations.Add([pscustomobject]@{
        RelativePath = $relativePath
        Source = $source
        Destination = $destination
        LinkType = $linkType
    })
}

foreach ($item in $skipped) {
    Write-Output "[SKIPPED] $($item.RelativePath) - the destination already exists."
}

foreach ($operation in $operations) {
    $action = "Create $($operation.LinkType) from $($operation.Source)"
    if (-not $PSCmdlet.ShouldProcess($operation.Destination, $action)) {
        continue
    }

    $newItemParameters = @{
        ItemType = $operation.LinkType
        Path = $operation.Destination
        Target = $operation.Source
    }
    $null = New-Item @newItemParameters

    $created = Get-Item -Force -LiteralPath $operation.Destination
    if ($created.LinkType -ne $operation.LinkType) {
        throw "Failed to verify the created link: $($operation.RelativePath)"
    }

    Write-Output "[LINKED] $($operation.RelativePath) ($($operation.LinkType)) <- $($operation.Source)"
}
