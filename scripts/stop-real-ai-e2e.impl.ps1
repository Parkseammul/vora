[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$BackendPid,
    [Parameter(Mandatory = $true)][int]$CeleryPid
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$root = (Split-Path -Parent $PSScriptRoot).TrimEnd('\')
$queue = 'vora_real_ai_e2e_20261030'

function Get-ProcessSnapshot { @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -gt 0 }) }

function Get-DescendantProcesses([object[]]$All, [int]$ParentId) {
    $children = @($All | Where-Object { [int]$_.ParentProcessId -eq $ParentId })
    $result = @($children)
    foreach ($child in $children) { $result += Get-DescendantProcesses $All ([int]$child.ProcessId) }
    return @($result)
}

function Get-CreationTime([object]$Process) {
    if ($Process.CreationDate -is [datetime]) { return [datetime]$Process.CreationDate }
    [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Process.CreationDate)
}

function Test-ProjectCommand([object]$Process, [string]$Pattern) {
    return ($null -ne $Process.CommandLine -and $Process.CommandLine -match [Regex]::Escape($root) -and $Process.CommandLine -match $Pattern)
}

function Assert-ProcessIdentity([object]$Process, [string]$Role, [string]$Pattern) {
    if ($null -eq $Process -or -not (Test-ProjectCommand $Process $Pattern)) {
        throw "$Role is not a recognized process for this VORA project; aborting without termination."
    }
}

function Assert-SameProcess([object]$Expected, [object]$Current, [string]$Role) {
    if ($null -eq $Current -or [string]$Expected.CreationDate -ne [string]$Current.CreationDate) {
        throw "$Role PID $($Expected.ProcessId) was reused or changed; aborting without termination."
    }
}

function Add-UniqueProcess([System.Collections.ArrayList]$List, [object]$Process) {
    if ($null -ne $Process -and -not (@($List | Where-Object { $_.ProcessId -eq $Process.ProcessId }).Count)) { [void]$List.Add($Process) }
}

function Stop-ValidatedTree([object[]]$All, [object[]]$Roots) {
    $targets = New-Object System.Collections.ArrayList
    foreach ($rootProcess in $Roots) {
        Add-UniqueProcess $targets $rootProcess
        foreach ($child in @(Get-DescendantProcesses $All ([int]$rootProcess.ProcessId))) { Add-UniqueProcess $targets $child }
    }
    $latest = Get-ProcessSnapshot
    foreach ($target in @($targets)) {
        $current = @($latest | Where-Object { $_.ProcessId -eq $target.ProcessId }) | Select-Object -First 1
        Assert-SameProcess $target $current 'Termination target'
    }
    foreach ($target in @($targets | Sort-Object { Get-CreationTime $_ } -Descending) ) { Stop-Process -Id ([int]$target.ProcessId) -Force }
    return @($targets | Select-Object -ExpandProperty ProcessId)
}

$all = Get-ProcessSnapshot
$backend = @($all | Where-Object { $_.ProcessId -eq $BackendPid }) | Select-Object -First 1
$celery = @($all | Where-Object { $_.ProcessId -eq $CeleryPid }) | Select-Object -First 1
$backendIsLauncher = $null -ne $backend -and (Test-ProjectCommand $backend 'run_real_ai_backend\.py')
$backendIsDirect = $null -ne $backend -and (Test-ProjectCommand $backend 'app\.main:app')
if (-not ($backendIsLauncher -or $backendIsDirect)) { throw "Backend PID $BackendPid is not the project launcher or app.main:app; aborting without termination." }
Assert-ProcessIdentity $celery 'Celery' 'app\.celery_app'
if ($celery.CommandLine -notmatch [Regex]::Escape($queue)) { throw 'Celery is not consuming the dedicated queue; aborting without termination.' }

$listener = $null
if ($backendIsLauncher) {
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 8000 })
    if ($listeners.Count -ne 1) { throw 'Expected exactly one Backend listener on port 8000; aborting without termination.' }
    $listener = @($all | Where-Object { $_.ProcessId -eq [int]$listeners[0].OwningProcess }) | Select-Object -First 1
    if ($null -eq $listener -or $listener.CommandLine -notmatch 'run_real_ai_backend\.py') {
        throw 'Backend listener is not the project launcher runtime; aborting without termination.'
    }
    if ([int]$listener.ParentProcessId -ne $BackendPid) { throw 'Backend listener is not a child of the specified launcher; aborting without termination.' }
    $delta = [Math]::Abs(((Get-CreationTime $listener) - (Get-CreationTime $backend)).TotalSeconds)
    if ($delta -gt 2) { throw 'Backend launcher/listener start times differ unexpectedly; aborting without termination.' }
} else {
    $listener = $backend
    $portOwner = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 8000 })
    if ($portOwner.Count -ne 1 -or [int]$portOwner[0].OwningProcess -ne $BackendPid) { throw 'Direct Backend does not own port 8000; aborting without termination.' }
}

foreach ($child in @(Get-DescendantProcesses $all $BackendPid)) {
    $isBackendRuntime = $child.ProcessId -eq $listener.ProcessId -and $child.CommandLine -match 'run_real_ai_backend\.py'
    $isWindowsConsole = (
        $child.Name -ieq 'conhost.exe' -and
        $child.ExecutablePath -match '(?i)\\Windows\\System32\\conhost\.exe$' -and
        [int]$child.ParentProcessId -eq $BackendPid -and
        [Math]::Abs(((Get-CreationTime $child) - (Get-CreationTime $backend)).TotalSeconds) -le 2
    )
    $isDirectBackend = $child.CommandLine -match 'app\.main:app' -and $child.CommandLine -match [Regex]::Escape($root)
    if (-not ($isBackendRuntime -or $isWindowsConsole -or $isDirectBackend)) { throw "Backend child PID $($child.ProcessId) is not recognized; aborting without termination." }
}
foreach ($child in @(Get-DescendantProcesses $all $CeleryPid)) {
    $isCeleryWorker = (
        $child.Name -match '^python(\.exe)?$' -and
        $child.CommandLine -match 'celery' -and
        $child.CommandLine -match [Regex]::Escape($queue)
    )
    $isCeleryConsole = (
        $child.Name -ieq 'conhost.exe' -and
        $child.ExecutablePath -match '(?i)\\Windows\\System32\\conhost\.exe$' -and
        [int]$child.ParentProcessId -eq $CeleryPid -and
        [Math]::Abs(((Get-CreationTime $child) - (Get-CreationTime $celery)).TotalSeconds) -le 2
    )
    if (-not ($isCeleryWorker -or $isCeleryConsole)) { throw "Celery child PID $($child.ProcessId) is not recognized; aborting without termination." }
}

$stoppedBackend = Stop-ValidatedTree $all @($backend, $listener)
$stoppedCelery = Stop-ValidatedTree $all @($celery)
Write-Host "Stopped VORA real-AI E2E Backend tree: $($stoppedBackend -join ', ')"
Write-Host "Stopped VORA real-AI E2E Celery tree: $($stoppedCelery -join ', ')"
