# One-line installer for the SSH Server Manager Agent Skill (Windows).
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.ps1 | iex"
#
# or from a checkout:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# Overrides: $env:SSM_REPO_URL, $env:SSM_INSTALL_DIR, $env:SSM_SKILLS_DIRS (';'-separated).
$ErrorActionPreference = "Stop"

$SkillName = "ssh-server-manager"
$RepoUrl = if ($env:SSM_REPO_URL) { $env:SSM_REPO_URL } else { "https://github.com/xiayh0107/servers-connect.git" }
$InstallDir = if ($env:SSM_INSTALL_DIR) { $env:SSM_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "servers-connect" }

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python (3.10+) is required - install from python.org or the Microsoft Store"
}

# 1. Locate or fetch the source tree.
$Source = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "$SkillName\SKILL.md"))) {
    $Source = Join-Path $PSScriptRoot $SkillName
    Write-Host "Using source checkout: $Source"
} elseif (Test-Path (Join-Path $InstallDir "$SkillName\SKILL.md")) {
    $Source = Join-Path $InstallDir $SkillName
    if ((Test-Path (Join-Path $InstallDir ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "Updating existing install in $InstallDir"
        git -C $InstallDir pull --ff-only
    }
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required to fetch $RepoUrl" }
    Write-Host "Cloning $RepoUrl -> $InstallDir"
    git clone --depth 1 $RepoUrl $InstallDir
    $Source = Join-Path $InstallDir $SkillName
}

# 2. Dependencies.
Write-Host "Installing Python dependencies into $Source\.venv ..."
python (Join-Path $Source "scripts\bootstrap.py") | Out-Null

# 3. Link into agent skills directories (junctions need no admin rights).
$candidates = @(
    (Join-Path $env:USERPROFILE ".claude\skills"),
    (Join-Path $env:USERPROFILE ".codex\skills")
)
if ($env:SSM_SKILLS_DIRS) { $candidates += $env:SSM_SKILLS_DIRS -split ";" }

$linked = @()
foreach ($dir in $candidates) {
    $parent = Split-Path $dir -Parent
    if (-not (Test-Path $parent)) { continue }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $target = Join-Path $dir $SkillName
    if (Test-Path $target) {
        $item = Get-Item $target -Force
        if ($item.LinkType) { Remove-Item $target -Force }
        else { Write-Host "skip: $target exists and is not a link"; continue }
    }
    New-Item -ItemType Junction -Path $target -Value $Source | Out-Null
    $linked += $target
}

if ($linked.Count) { Write-Host "Skill linked into:`n  $($linked -join "`n  ")" }
else { Write-Host "No agent skills directory detected. Link manually:`n  New-Item -ItemType Junction -Path `"$env:USERPROFILE\.claude\skills\$SkillName`" -Value `"$Source`"" }

# 4. Health check.
Write-Host ""
Write-Host "Running serverctl doctor ..."
& (Join-Path $Source "scripts\serverctl.cmd") doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "doctor reported issues above - see $Source\docs\installation.md"
    exit 1
}
Write-Host ""
Write-Host "Done. Your agent can now use the '$SkillName' skill."
