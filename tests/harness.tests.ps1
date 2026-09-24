[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$source = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts/harness.ps1'
$projectPython = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv/Scripts/python.exe'
$tokens = $null
$errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('vora harness ' + [guid]::NewGuid())
$oldPath = $env:PATH
$oldFailure = $env:VORA_HARNESS_TEST_FAILURE
$oldLog = $env:VORA_HARNESS_TEST_LOG
$hostExe = Join-Path $PSHOME 'powershell.exe'
$failures = 0
try {
    foreach ($dir in @('scripts', 'backend', 'frontend', 'bin', 'caller', 'empty')) {
        New-Item -ItemType Directory -Path (Join-Path $fixture $dir) -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $fixture 'scripts/harness.ps1')
    $mock = @'
@echo off
if "%~1"=="-c" (
echo %~f0
exit /b 0
)
echo %~n0:%*^|%CD%>>"%VORA_HARNESS_TEST_LOG%"
if "%VORA_HARNESS_TEST_FAILURE%"=="%~n0:%*" exit /b 7
exit /b 0
'@
    foreach ($name in @('python', 'npm', 'git')) {
        Set-Content -Encoding ASCII -LiteralPath (Join-Path $fixture "bin/$name.cmd") -Value $mock
    }
    # Real venv and fake modules exercise interpreter selection without backend dependencies.
    & $projectPython -m venv --without-pip (Join-Path $fixture '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the regression Python 3.12 venv.' }
    $gateModule = @'
import os
import sys
from pathlib import Path

call = "python:-m " + Path(__file__).stem
if sys.argv[1:]:
    call += " " + " ".join(sys.argv[1:])
with open(os.environ["VORA_HARNESS_TEST_LOG"], "a") as log:
    log.write(call + "|" + os.getcwd() + "\n")
sys.exit(7 if os.environ.get("VORA_HARNESS_TEST_FAILURE") == call else 0)
'@
    foreach ($module in @('pytest', 'ruff', 'mypy')) {
        Set-Content -Encoding ASCII -LiteralPath (Join-Path $fixture "backend/$module.py") -Value $gateModule
    }
    # PATH Python must never be selected.
    Set-Content -Encoding ASCII -LiteralPath (Join-Path $fixture 'bin/python.cmd') -Value @('@echo off', 'exit /b 99')
    $env:VORA_HARNESS_TEST_LOG = Join-Path $fixture 'calls.txt'
    $cases = @(
        @('Backend', ''), @('Frontend', ''), @('All', ''),
        @('All', 'python:-m pytest'), @('All', 'python:-m ruff check .'),
        @('All', 'python:-m mypy app'), @('All', 'npm:run lint'),
        @('All', 'npm:test'), @('All', 'npm:run build'),
        @('All', 'git:diff --check'), @('All', 'git:diff --cached --check'),
        @('Invalid', ''), @('All', 'missing-tools'), @('Backend', 'missing-python')
    )
    Push-Location -LiteralPath (Join-Path $fixture 'caller')
    try {
        foreach ($case in $cases) {
            $scope, $failure = $case
            Set-Content -LiteralPath $env:VORA_HARNESS_TEST_LOG -Value $null
            $env:VORA_HARNESS_TEST_FAILURE = $failure
            $env:PATH = if ($failure -eq 'missing-tools') {
                Join-Path $fixture 'empty'
            } else { Join-Path $fixture 'bin' }
            $fixturePython = Join-Path $fixture '.venv/Scripts/python.exe'
            if ($failure -eq 'missing-python') {
                Rename-Item -LiteralPath $fixturePython -NewName 'python.disabled'
            }
            $expected = @()
            if ($scope -in @('Backend', 'All')) {
                $expected += @('python:-m pytest', 'python:-m ruff check .', 'python:-m mypy app')
            }
            if ($scope -in @('Frontend', 'All')) {
                $expected += @('npm:run lint', 'npm:test', 'npm:run build')
            }
            $expected += @('git:diff --check', 'git:diff --cached --check')
            if ($scope -eq 'Invalid') { $expected = @() }
            if ($failure -eq 'missing-tools') {
                $expected = @('python:-m pytest', 'python:-m ruff check .', 'python:-m mypy app')
            }
            if ($failure -eq 'missing-python') {
                $expected = @('git:diff --check', 'git:diff --cached --check')
            }
            $ErrorActionPreference = 'Continue'
            $output = & $hostExe -NoProfile -ExecutionPolicy Bypass -File (
                Join-Path $fixture 'scripts/harness.ps1'
            ) -Scope $scope 2>&1
            $code = $LASTEXITCODE
            if ($failure -eq 'missing-python') {
                Rename-Item -LiteralPath (Join-Path $fixture '.venv/Scripts/python.disabled') -NewName 'python.exe'
            }
            $ErrorActionPreference = 'Stop'
            $calls = @(Get-Content -LiteralPath $env:VORA_HARNESS_TEST_LOG)
            $expectFailure = $failure -ne '' -or $scope -eq 'Invalid'
            $valid = (($code -eq 0) -eq (-not $expectFailure)) -and ($calls.Count -eq $expected.Count)
            for ($i = 0; $i -lt $calls.Count; $i++) {
                $parts = $calls[$i] -split '\|', 2
                $directory = if ($parts[0].StartsWith('python:')) {
                    Join-Path $fixture 'backend'
                } elseif ($parts[0].StartsWith('npm:')) {
                    Join-Path $fixture 'frontend'
                } else { $fixture }
                $valid = $valid -and ($parts[0] -eq $expected[$i]) -and ($parts[1] -eq $directory)
            }
            if ($scope -ne 'Invalid') {
                $summary = if ($expectFailure) { '[FAIL] Harness' } else { '[PASS] Harness' }
                $valid = $valid -and (($output | Out-String).Contains($summary))
            }
            if ($failure -ne '' -and $failure -notin @('missing-tools', 'missing-python')) {
                $valid = $valid -and (($output | Out-String).Contains('(exit 7)'))
            }
            if ($valid) { Write-Host "[PASS] Scope=$scope failure=$failure" }
            else {
                $failures++
                Write-Host "[FAIL] Scope=$scope failure=$failure exit=$code"
                Write-Host ($output | Out-String)
            }
        }
    } finally { Pop-Location }
} finally {
    $env:PATH = $oldPath
    $env:VORA_HARNESS_TEST_FAILURE = $oldFailure
    $env:VORA_HARNESS_TEST_LOG = $oldLog
    # Only remove the unique fixture under the resolved temporary directory.
    $resolved = [IO.Path]::GetFullPath($fixture)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolved) -notmatch '^vora harness [0-9a-f-]{36}$') {
        throw 'Unsafe fixture cleanup path'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
if ($failures) { exit 1 }
Write-Host '[PASS] Harness regression checks (14 cases); production gates are separate.'
exit 0
