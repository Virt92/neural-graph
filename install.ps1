# Neural Graph installer
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$GraphDir = Join-Path $RepoRoot 'memory\graph'
$ElectronDir = Join-Path $GraphDir 'electron'
$HookHandler = Join-Path $GraphDir 'hook_handler.py'
$LaunchBat = Join-Path $GraphDir 'launch.bat'
$NightlyBat = Join-Path $GraphDir 'nightly.bat'
$ClaudeSettings = Join-Path $env:USERPROFILE '.claude\settings.json'

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " Neural Graph - installer" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Repo root: $RepoRoot"
Write-Host ""

# --- 1. Prereq check ---
function Test-Cmd($name) {
    $null = Get-Command $name -ErrorAction SilentlyContinue
    return $?
}

Write-Host "[1/6] Checking prerequisites..." -ForegroundColor Yellow
$missing = @()
if (-not (Test-Cmd 'py') -and -not (Test-Cmd 'python')) { $missing += 'Python 3.10+' }
if (-not (Test-Cmd 'node')) { $missing += 'Node 18+' }
if (-not (Test-Cmd 'npm')) { $missing += 'npm' }
if (-not (Test-Cmd 'git')) { $missing += 'Git (optional but recommended)' }
if (-not (Test-Path $ClaudeSettings)) {
    Write-Host "  [warn] $ClaudeSettings not found - is Claude Code installed?" -ForegroundColor Yellow
}
if ($missing.Count -gt 0) {
    Write-Host "  [error] Missing: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "  Install them and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "  ok" -ForegroundColor Green

# --- 2. npm install ---
Write-Host ""
Write-Host "[2/6] npm install in electron/..." -ForegroundColor Yellow
Push-Location $ElectronDir
try {
    npm install --silent 2>&1 | Out-Null
    Write-Host "  ok" -ForegroundColor Green
} catch {
    Write-Host "  [error] npm install failed: $_" -ForegroundColor Red
    Pop-Location
    exit 1
}
Pop-Location

# --- 3. pip install ---
Write-Host ""
Write-Host "[3/6] pip install requirements..." -ForegroundColor Yellow
$pipCmd = if (Test-Cmd 'py') { 'py -m pip' } else { 'python -m pip' }
$reqFile = Join-Path $RepoRoot 'requirements.txt'
try {
    Invoke-Expression "$pipCmd install -r `"$reqFile`" --quiet" 2>&1 | Out-Null
    Write-Host "  ok" -ForegroundColor Green
} catch {
    Write-Host "  [warn] pip install had issues: $_" -ForegroundColor Yellow
}

# --- 4. Merge hooks into ~/.claude/settings.json ---
Write-Host ""
Write-Host "[4/6] Merging hooks into Claude settings..." -ForegroundColor Yellow
if (Test-Path $ClaudeSettings) {
    try {
        $settings = Get-Content $ClaudeSettings -Raw | ConvertFrom-Json
    } catch {
        Write-Host "  [error] Could not parse $ClaudeSettings - leaving alone." -ForegroundColor Red
        Write-Host "  Add hooks manually using claude-config\settings.hooks.example.json" -ForegroundColor Yellow
        $settings = $null
    }

    if ($null -ne $settings) {
        $hookCmd = "py `"$HookHandler`""
        $events = @('SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','Stop','SessionEnd')

        if (-not $settings.PSObject.Properties.Match('hooks').Count) {
            $settings | Add-Member -NotePropertyName 'hooks' -NotePropertyValue ([PSCustomObject]@{})
        }

        foreach ($ev in $events) {
            $entry = if ($ev -eq 'PreToolUse' -or $ev -eq 'PostToolUse') {
                [PSCustomObject]@{
                    matcher = '.*'
                    hooks = @([PSCustomObject]@{ type = 'command'; command = $hookCmd })
                }
            } else {
                [PSCustomObject]@{
                    hooks = @([PSCustomObject]@{ type = 'command'; command = $hookCmd })
                }
            }
            $existing = $settings.hooks.PSObject.Properties.Match($ev).Count
            if ($existing -eq 0) {
                $settings.hooks | Add-Member -NotePropertyName $ev -NotePropertyValue @($entry)
            } else {
                # Replace if our handler not present
                $arr = @($settings.hooks.$ev)
                $hasOurs = $false
                foreach ($block in $arr) {
                    foreach ($h in $block.hooks) {
                        if ($h.command -like "*hook_handler.py*") { $hasOurs = $true }
                    }
                }
                if (-not $hasOurs) {
                    $settings.hooks.$ev = @($arr + $entry)
                }
            }
        }

        $backupPath = "$ClaudeSettings.backup-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item $ClaudeSettings $backupPath
        $settings | ConvertTo-Json -Depth 30 | Set-Content $ClaudeSettings -Encoding UTF8
        Write-Host "  ok (backup: $backupPath)" -ForegroundColor Green
    }
} else {
    Write-Host "  [skip] $ClaudeSettings not found" -ForegroundColor Yellow
}

# --- 5. Task Scheduler nightly ---
Write-Host ""
Write-Host "[5/6] Registering Task Scheduler nightly job..." -ForegroundColor Yellow
$taskName = 'NeuralGraphNightly'
try {
    schtasks /Query /TN $taskName 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        schtasks /Delete /TN $taskName /F 2>&1 | Out-Null
    }
    schtasks /Create /SC DAILY /ST 03:13 /TN $taskName /TR "`"$NightlyBat`"" /F 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ok (runs 03:13 daily)" -ForegroundColor Green
    } else {
        Write-Host "  [warn] Task Scheduler registration failed (exit $LASTEXITCODE)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  [warn] $_" -ForegroundColor Yellow
}

# --- 6. Desktop shortcut ---
Write-Host ""
Write-Host "[6/6] Creating desktop shortcut..." -ForegroundColor Yellow
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Neural Graph.lnk'
try {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($shortcutPath)
    $sc.TargetPath = $LaunchBat
    $sc.WorkingDirectory = $GraphDir
    $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $sc.Save()
    Write-Host "  ok ($shortcutPath)" -ForegroundColor Green
} catch {
    Write-Host "  [warn] $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " Done. Next steps:" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  1. Restart any running Claude Code sessions (so new hooks load)"
Write-Host "  2. Double-click 'Neural Graph' on Desktop, or run launch.bat"
Write-Host "  3. Start a Claude session in any project - your core ghost appears"
Write-Host ""
