[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [int]$BackendPid,
    [Parameter(Mandatory = $true)]
    [int]$CeleryPid
)

& (Join-Path $PSScriptRoot 'stop-real-ai-e2e.impl.ps1') -BackendPid $BackendPid -CeleryPid $CeleryPid
exit $LASTEXITCODE

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$expected = @($BackendPid, $CeleryPid)
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $expected -contains $_.ProcessId
})

if ($processes.Count -ne 2) {
    throw '지정한 두 PID가 모두 현재 프로세스로 존재하지 않아 종료를 중단합니다.'
}

foreach ($process in $processes) {
    $isVora = $process.CommandLine -match [Regex]::Escape($repoRoot) -and
        ($process.CommandLine -match 'app\.main:app' -or $process.CommandLine -match 'app\.celery_app')
    if (-not $isVora) {
        throw "PID $($process.ProcessId)가 VORA의 새 Backend/Celery 프로세스가 아니어서 종료를 중단합니다."
    }
}

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force
}

Write-Host "Stopped VORA real-AI E2E processes: Backend PID $BackendPid, Celery PID $CeleryPid"
