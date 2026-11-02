<#
.SYNOPSIS
    Install or update Falcon MCP and register it with the agent apps already on this PC, best-effort and
    idempotent. The agent apps themselves (Claude Desktop, Claude Code, Codex) are never installed or updated.

.DESCRIPTION
    Steps (each one is independent; a failure is reported and the next step still runs):
      1. Python 3.11+ (installed with winget only when none is found).
      2. Repository virtual environment .venv with falcon-mcp installed editable, plus extras.
      3. Runtime home <repo>\.runtime: FALCON_MCP_HOME user variable and "falcon_mcp prepare".
      4. Detect installed agents: Claude Desktop (profile folder), Claude Code (claude command,
         ~/.claude.json or ~/.claude), Codex (codex command or ~/.codex). Missing agents are skipped.
      5. Agent skill skills\falcon-instrument-workflow: built with scripts\build_skill.py (generated
         reference index) into .runtime\skill-build, copied for each detected Claude Code / Codex, and
         zipped to .runtime\falcon-instrument-workflow.zip for upload in Claude Desktop.
      6. MCP configuration for each detected agent (scripts\configure_clients.py).
    Every configuration file and replaced skill folder is backed up to .runtime\backups\<timestamp>
    before it changes. Run with -DryRun first to see what would happen without changing anything.

.EXAMPLE
    .\install-windows.cmd -DryRun
.EXAMPLE
    .\install-windows.cmd -SkipCodex -Extras capture,host,feedback
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string[]]$Extras = @('capture', 'host'),
    [string]$RuntimeHome,
    [switch]$SkipPython,
    [switch]$SkipClaudeDesktop,
    [switch]$SkipClaudeCode,
    [switch]$SkipCodex,
    [switch]$SkipSkill,
    [switch]$SkipConfig,
    [switch]$NoWinget,
    [switch]$NoUserEnv
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $RuntimeHome) { $RuntimeHome = Join-Path $Repo '.runtime' }
$Venv = Join-Path $Repo '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$AudioPython = Join-Path $Repo '.audio-venv\Scripts\python.exe'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$BackupDir = Join-Path $RuntimeHome "backups\$Stamp"
$SkillName = 'falcon-instrument-workflow'
$SkillSource = Join-Path $Repo "skills\$SkillName"
$Results = New-Object System.Collections.Generic.List[object]

function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-Plan([string]$Message) { Write-Host "    [dry-run] $Message" -ForegroundColor Yellow }

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Test-Command([string]$Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Invoke-Native {
    # Runs an external command (or prints it in dry-run) and throws on a non-zero exit code.
    param([string]$File, [string[]]$Arguments = @(), [switch]$Always)
    $display = (@($File) + $Arguments | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    if ($DryRun -and -not $Always) { Write-Plan "run: $display"; return }
    Write-Info "> $display"
    $ErrorActionPreference = 'Continue'  # Native stderr (pip warnings, progress) must not abort the step.
    # 2>&1 plus string conversion shows stderr as plain text instead of NativeCommandError records.
    & $File @Arguments 2>&1 | ForEach-Object { "$_" } | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "'$display' exited with code $LASTEXITCODE" }
}

function Get-NativeText([string]$File, [string[]]$Arguments) {
    # Returns trimmed stdout of a quick query command, or $null when it fails.
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $File @Arguments 2>$null
        if ($LASTEXITCODE -eq 0) { return ($out | Out-String).Trim() }
    } catch { }
    return $null
}

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ''
    Write-Host "==> $Name" -ForegroundColor Cyan
    try {
        $note = & $Body
        $Results.Add([pscustomobject]@{ Step = $Name; Status = $(if ($DryRun) { 'planned' } else { 'ok' }); Note = "$note" })
    } catch {
        Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
        $Results.Add([pscustomobject]@{ Step = $Name; Status = 'failed'; Note = $_.Exception.Message })
    }
}

function Skip-Step([string]$Name, [string]$Why) {
    $Results.Add([pscustomobject]@{ Step = $Name; Status = 'skipped'; Note = $Why })
}

function Install-WithWinget([string]$Id) {
    if ($NoWinget) { throw "$Id is missing and -NoWinget was given" }
    if (-not (Test-Command 'winget')) { throw "$Id is missing and winget is not available; install it manually" }
    Invoke-Native 'winget' @('install', '--exact', '--id', $Id, '--silent', '--accept-package-agreements',
        '--accept-source-agreements')
    if (-not $DryRun) { Update-SessionPath }
}

function Find-Python {
    # Returns @{Exe; Prefix; Version} for the first Python >= 3.11 found, or $null.
    $candidates = @()
    if (Test-Command 'py') {
        foreach ($tag in '-3.13', '-3.12', '-3.11') { $candidates += @{ Exe = 'py'; Prefix = @($tag) } }
    }
    if (Test-Command 'python') { $candidates += @{ Exe = 'python'; Prefix = @() } }
    foreach ($candidate in $candidates) {
        # No quotes inside -c: Windows PowerShell 5.1 and 7.x pass embedded quotes differently.
        $query = @($candidate.Prefix) + @('-c', 'import sys; print(*sys.version_info[:2], sep=chr(46))')
        $version = Get-NativeText $candidate.Exe $query
        if ($version -match '^\d+\.\d+$' -and [version]$version -ge [version]'3.11') {
            $candidate.Version = $version
            return $candidate
        }
    }
    return $null
}

function Backup-Path([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($DryRun) { Write-Plan "back up $Path"; return }
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    Copy-Item -LiteralPath $Path -Destination (Join-Path $BackupDir $Label) -Recurse -Force
    Write-Info "backup: $(Join-Path $BackupDir $Label)"
}

function Get-TreeHashes([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $base = (Resolve-Path -LiteralPath $Path).Path
    (Get-ChildItem -LiteralPath $base -Recurse -File | Sort-Object FullName | ForEach-Object {
        "$($_.FullName.Substring($base.Length)) $((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)"
    }) -join "`n"
}

function Install-Skill([string]$Root, [string]$Label, [string]$Built) {
    $target = Join-Path $Root $SkillName
    if ((Get-TreeHashes $target) -eq (Get-TreeHashes $Built)) { Write-Info "up to date: $target"; return }
    Backup-Path $target "skill-$Label"
    if ($DryRun) { Write-Plan "replace $target with the built skill"; return }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Copy-Item -LiteralPath $Built -Destination $target -Recurse -Force
    Write-Info "installed: $target"
}

function Get-ClaudeDesktopConfigs {
    # Standard installer profile plus any Microsoft Store (MSIX) profile; existing files first.
    $paths = @()
    $standardDir = Join-Path $env:APPDATA 'Claude'
    $paths += Join-Path $standardDir 'claude_desktop_config.json'
    $packages = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path $packages) {
        Get-ChildItem $packages -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue | ForEach-Object {
            $dir = Join-Path $_.FullName 'LocalCache\Roaming\Claude'
            if (Test-Path $dir) { $paths += Join-Path $dir 'claude_desktop_config.json' }
        }
    }
    $found = @($paths | Where-Object { (Test-Path $_) -or (Test-Path (Split-Path $_)) })
    return $found
}

Write-Host "Falcon MCP Windows installer$(if ($DryRun) { ' (dry run: nothing will be changed)' })" -ForegroundColor Green
Write-Host "    repository:      $Repo"
Write-Host "    FALCON_MCP_HOME: $RuntimeHome"
Write-Host "    extras:          $($Extras -join ',')"

$running = @(Get-Process -Name 'claude', 'Claude', 'codex' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host ''
    Write-Host '    Note: Claude Desktop, Claude Code or Codex is running. Configuration changes take effect' -ForegroundColor Yellow
    Write-Host '    after a full restart; Claude Desktop may rewrite its config file while it is open.' -ForegroundColor Yellow
}

$Python = $null
if ($SkipPython) { Skip-Step 'Python and falcon-mcp package' '-SkipPython' } else {
    Invoke-Step 'Python 3.11+' {
        $script:Python = Find-Python
        if (-not $script:Python) {
            Install-WithWinget 'Python.Python.3.12'
            if ($DryRun) { return 'would install Python 3.12' }
            $script:Python = Find-Python
            if (-not $script:Python) { throw 'Python was installed but is not on PATH yet; open a new terminal and rerun' }
        }
        Write-Info "found: $($script:Python.Exe) $($script:Python.Prefix -join ' ') (Python $($script:Python.Version))"
        "Python $($script:Python.Version)"
    }

    Invoke-Step 'falcon-mcp package in .venv' {
        if (-not (Test-Path $VenvPython)) {
            if (-not $script:Python) { throw 'no Python 3.11+ available to create .venv' }
            Invoke-Native $script:Python.Exe (@($script:Python.Prefix) + @('-m', 'venv', $Venv))
        } else { Write-Info "reusing $Venv" }
        Invoke-Native $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
        $spec = if ($Extras.Count -gt 0) { "$Repo[$($Extras -join ',')]" } else { $Repo }
        # Without --upgrade, pip keeps satisfied dependencies, so DLLs loaded by a running server stay untouched.
        Invoke-Native $VenvPython @('-m', 'pip', 'install', '-e', $spec)
        if (-not $DryRun) { "falcon-mcp $(Get-NativeText $VenvPython @('-c', 'import falcon_mcp; print(falcon_mcp.__version__)'))" }
    }
}

Invoke-Step 'Runtime home and bridge files' {
    if (-not $NoUserEnv) {
        $current = [Environment]::GetEnvironmentVariable('FALCON_MCP_HOME', 'User')
        if ($current -ne $RuntimeHome) {
            if ($DryRun) { Write-Plan "set user variable FALCON_MCP_HOME=$RuntimeHome (was '$current')" }
            else {
                [Environment]::SetEnvironmentVariable('FALCON_MCP_HOME', $RuntimeHome, 'User')
                Write-Info "user variable FALCON_MCP_HOME: '$current' -> '$RuntimeHome'"
            }
        } else { Write-Info 'user variable FALCON_MCP_HOME already set' }
    }
    $env:FALCON_MCP_HOME = $RuntimeHome
    if ($DryRun) { Write-Plan "run: $VenvPython -m falcon_mcp prepare"; return }
    if (-not (Test-Path $VenvPython)) { throw '.venv is missing; the package step must succeed first' }
    New-Item -ItemType Directory -Force -Path $RuntimeHome | Out-Null
    if ($null -eq (Get-NativeText $VenvPython @('-m', 'falcon_mcp', 'prepare'))) { throw 'falcon_mcp prepare failed' }
    Write-Info "bridge launcher: $(Join-Path $RuntimeHome 'bridge\falcon_bridge.lua')"
    'prepared'
}

$ClaudeRoot = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
$ClaudeJson = if ($env:CLAUDE_CONFIG_DIR) { Join-Path $env:CLAUDE_CONFIG_DIR '.claude.json' } else { Join-Path $env:USERPROFILE '.claude.json' }
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$DesktopConfigs = @()
$HasClaudeCode = $false
$HasCodex = $false
Invoke-Step 'Detect installed agents' {
    $found = @()
    if (-not $SkipClaudeDesktop) {
        $script:DesktopConfigs = @(Get-ClaudeDesktopConfigs)
        if ($script:DesktopConfigs.Count -gt 0) { $found += "Claude Desktop ($($script:DesktopConfigs.Count) profile(s))" }
    }
    if (-not $SkipClaudeCode) {
        # Claude Desktop's built-in Code mode also uses ~/.claude, so the folder counts without the CLI.
        $script:HasClaudeCode = (Test-Command 'claude') -or (Test-Path $ClaudeJson) -or (Test-Path $ClaudeRoot)
        if ($script:HasClaudeCode) { $found += 'Claude Code' }
    }
    if (-not $SkipCodex) {
        $script:HasCodex = (Test-Command 'codex') -or (Test-Path $CodexHome)
        if ($script:HasCodex) { $found += 'Codex' }
    }
    foreach ($item in $found) { Write-Info "found: $item" }
    if ($found.Count -eq 0) { Write-Info 'no supported agent found; only the server package was installed' }
    $(if ($found.Count -gt 0) { $found -join ', ' } else { 'none' })
}

if ($SkipSkill) { Skip-Step 'Agent skill' '-SkipSkill' } else {
    Invoke-Step 'Agent skill' {
        if (-not (Test-Path (Join-Path $SkillSource 'SKILL.md'))) { throw "missing $SkillSource\SKILL.md" }
        if (-not ($HasClaudeCode -or $HasCodex -or $DesktopConfigs.Count)) { return 'no agent to install for' }
        if (-not (Test-Path $VenvPython)) {
            if ($DryRun) { Write-Plan 'build and install the skill once .venv exists'; return }
            throw '.venv is missing; cannot build the skill'
        }
        # The tracked skill has no generated content; build_skill.py adds the reference index.
        # A dry run builds into a temporary folder so it can still compare installed copies.
        $buildRoot = if ($DryRun) { Join-Path ([IO.Path]::GetTempPath()) 'falcon-mcp-skill-build' } else { Join-Path $RuntimeHome 'skill-build' }
        Invoke-Native $VenvPython @((Join-Path $Repo 'scripts\build_skill.py'), '--out', $buildRoot) -Always
        $built = Join-Path $buildRoot $SkillName
        $done = @()
        if ($HasClaudeCode) { Install-Skill (Join-Path $ClaudeRoot 'skills') 'claude-code' $built; $done += 'Claude Code' }
        if ($HasCodex) { Install-Skill (Join-Path $env:USERPROFILE '.agents\skills') 'codex' $built; $done += 'Codex' }
        if ($DesktopConfigs.Count -eq 0) { return "$($done -join ', ')" }
        $zip = Join-Path $RuntimeHome "$SkillName.zip"
        if ($DryRun) { Write-Plan "build $zip for Claude Desktop upload"; return }
        # build_skill.py writes forward-slash entry names; Windows PowerShell 5.1 Compress-Archive does not.
        Invoke-Native $VenvPython @((Join-Path $Repo 'scripts\build_skill.py'), '--out', $buildRoot, '--zip', $zip) -Always
        Write-Info "Claude Desktop: Customize > Skills > Add skill > Upload a skill, then pick $zip"
        (@($done) + @('ZIP for Claude Desktop')) -join ', '
    }
}

if ($SkipConfig) { Skip-Step 'MCP client configuration' '-SkipConfig' } else {
    Invoke-Step 'MCP client configuration' {
        if (-not ($DesktopConfigs.Count -or $HasClaudeCode -or $HasCodex)) { return 'no agent to configure' }
        if ($DryRun -and -not (Test-Path $VenvPython)) { Write-Plan 'merge falcon entries once .venv exists'; return }
        if (-not (Test-Path $VenvPython)) { throw '.venv is missing; the package step must succeed first' }
        $arguments = @((Join-Path $Repo 'scripts\configure_clients.py'), '--python', $VenvPython, '--home', $RuntimeHome,
            '--repo', $Repo, '--backup-dir', $BackupDir)
        if (Test-Path $AudioPython) { $arguments += @('--audio-python', $AudioPython) }
        foreach ($path in $DesktopConfigs) { $arguments += @('--claude-desktop', $path) }
        if ($HasClaudeCode) { $arguments += @('--claude-code', $ClaudeJson) }
        if ($HasCodex) { $arguments += @('--codex', (Join-Path $CodexHome 'config.toml')) }
        if ($DryRun) { $arguments += '--dry-run' }
        Invoke-Native $VenvPython $arguments -Always
        'falcon entries merged for the detected agents'
    }
}

Write-Host ''
Write-Host 'Summary' -ForegroundColor Green
$Results | Format-Table -AutoSize -Wrap | Out-String -Width 160 | Write-Host
if (-not $DryRun -and (Test-Path $BackupDir)) { Write-Host "Backups: $BackupDir" }
Write-Host 'Next: fully quit and reopen the configured agent apps, then call falcon_status.'
Write-Host 'The Master bridge inside Falcon is a one-time manual step: see docs/one-time-setup.md.'
if (@($Results | Where-Object { $_.Status -eq 'failed' }).Count -gt 0) { exit 1 }
exit 0
