[CmdletBinding()]
param(
    [ValidateSet('Backend', 'Frontend', 'All')]
    [string]$Scope = 'Backend'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$results = New-Object 'System.Collections.Generic.List[object]'

function Invoke-Gate {
    param(
        [string]$Name,
        [string]$Directory,
        [string]$Command,
        [string[]]$CommandArguments
    )

    Write-Host ""
    Write-Host "[RUN] $Name : $Command $($CommandArguments -join ' ')"
    $code = 1
    $pushed = $false
    try {
        Push-Location -LiteralPath $Directory
        $pushed = $true
        if ($Command -eq 'python') {
            # Always use this checkout's venv, regardless of PATH or another active environment.
            $pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
            if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
                throw 'Root .venv Python is missing. Run: py -3.12 -m venv .venv'
            }
            $application = Get-Command $pythonPath -CommandType Application -ErrorAction Stop
            $version = & $application.Source -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'
            if ($LASTEXITCODE -ne 0) {
                throw 'Root .venv must use Python 3.12.'
            }
            Write-Host "[Python] $pythonPath ($version)"
        } else {
            # Only native applications are allowed; functions cannot mask a missing tool.
            $application = Get-Command $Command -CommandType Application -ErrorAction Stop |
                Select-Object -First 1
        }
        # Windows PowerShell may wrap native stderr as ErrorRecord. Exit code decides PASS.
        $ErrorActionPreference = 'Continue'
        & $application.Source @CommandArguments
        $code = $LASTEXITCODE
    }
    catch {
        Write-Host "[ERROR] $Name : $($_.Exception.Message)"
        $code = 1
    }
    finally {
        if ($pushed) { Pop-Location }
    }

    $status = if ($code -eq 0) { 'PASS' } else { 'FAIL' }
    $results.Add([pscustomobject]@{ Name = $Name; Status = $status; Code = $code })
    Write-Host "[$status] $Name (exit $code)"
}

Write-Host "VORA Harness | Scope: $Scope | Root: $repoRoot"
if ($Scope -in @('Backend', 'All')) {
    $backend = Join-Path $repoRoot 'backend'
    Invoke-Gate 'Backend pytest' $backend 'python' @('-m', 'pytest')
    Invoke-Gate 'Backend Ruff' $backend 'python' @('-m', 'ruff', 'check', '.')
    Invoke-Gate 'Backend mypy' $backend 'python' @('-m', 'mypy', 'app')
}
if ($Scope -in @('Frontend', 'All')) {
    $frontend = Join-Path $repoRoot 'frontend'
    Invoke-Gate 'Frontend lint' $frontend 'npm.cmd' @('run', 'lint')
    Invoke-Gate 'Frontend test' $frontend 'npm.cmd' @('test')
    Invoke-Gate 'Frontend build' $frontend 'npm.cmd' @('run', 'build')
}
Invoke-Gate 'Git working tree whitespace' $repoRoot 'git' @('diff', '--check')
Invoke-Gate 'Git staged whitespace' $repoRoot 'git' @('diff', '--cached', '--check')

Write-Host ""
Write-Host '=== Harness summary ==='
foreach ($result in $results) {
    Write-Host "[$($result.Status)] $($result.Name) (exit $($result.Code))"
}
Write-Host 'DB schema changes: run the separate Alembic gate in docs/harness/quality-gates.md.'
Write-Host 'Review untracked files separately; git diff does not include them.'
if (@($results | Where-Object { $_.Status -eq 'FAIL' }).Count -gt 0) {
    Write-Host '[FAIL] Harness'
    exit 1
}
Write-Host '[PASS] Harness'
exit 0
