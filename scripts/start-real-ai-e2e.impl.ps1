[CmdletBinding()]
param([int[]]$ExpectedProcessIds = @())
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$python = Join-Path $root '.venv\Scripts\python.exe'
$backendLauncher = 'scripts\run_real_ai_backend.py'
$storage = Join-Path $backend 'uploads\real-ai-e2e-20261030'
$evidenceRoot = Join-Path $storage '.e2e-preflight'
$evidencePath = Join-Path $evidenceRoot 'preflight-evidence.json'
$evidenceRuntimePath = Join-Path $evidenceRoot 'backend.runtime.json'
$rulesVersionId = 7291
$queue = 'vora_real_ai_e2e_20261030'
$worker = 'vora-real-ai-e2e@localhost'
$started = @()
$keyPtr = $null
$diagnosticDir = Join-Path $env:TEMP ("vora-real-ai-e2e-" + [Guid]::NewGuid().ToString('N'))
$diagnosticFiles = @()
$lastListenerObservation = '<none>'

function Clear-Secrets {
    if ($null -ne $script:keyPtr) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($script:keyPtr); $script:keyPtr = $null }
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:VORA_RUNTIME_DIAGNOSTIC_PATH -ErrorAction SilentlyContinue
}
function Stop-ProcessTree([int]$ProcessId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId")
    foreach ($child in $children) { Stop-ProcessTree ([int]$child.ProcessId) }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}
function Stop-Started {
    foreach ($p in @($script:started)) { if ($p -and -not $p.HasExited) { Stop-ProcessTree $p.Id } }
}
function Read-ProcessDiagnostics {
    foreach ($file in @($script:diagnosticFiles)) {
        if (Test-Path $file -PathType Leaf) {
            $content = Get-Content -LiteralPath $file -Raw -ErrorAction SilentlyContinue
            if ($content) { Write-Host "$(Split-Path $file -Leaf): $(Get-SafeDiagnostic $content)" }
        }
    }
}
function Clear-ProcessDiagnostics {
    if (Test-Path $script:diagnosticDir) { Remove-Item -LiteralPath $script:diagnosticDir -Recurse -Force -ErrorAction SilentlyContinue }
}
function Invoke-PythonStdin([string]$Name, [string]$Code) {
    $i = New-Object System.Diagnostics.ProcessStartInfo
    $i.FileName = $python; $i.Arguments = '-'; $i.WorkingDirectory = $backend
    $i.UseShellExecute = $false; $i.CreateNoWindow = $true
    $i.RedirectStandardInput = $true; $i.RedirectStandardOutput = $true; $i.RedirectStandardError = $true
    $p = New-Object System.Diagnostics.Process; $p.StartInfo = $i
    if (-not $p.Start()) { throw "Python validation could not start: $Name" }
    $p.StandardInput.Write($Code); $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd(); $null = $p.StandardError.ReadToEnd(); $p.WaitForExit()
    if ($p.ExitCode -ne 0) { throw "Python validation failed: $Name (exit $($p.ExitCode))" }
    if ($out.Trim()) { Write-Host $out.Trim() }
}
function Invoke-CeleryProbe([string]$Code) {
    $i = New-Object System.Diagnostics.ProcessStartInfo
    $i.FileName = $python; $i.Arguments = '-'; $i.WorkingDirectory = $backend
    $i.UseShellExecute = $false; $i.CreateNoWindow = $true
    $i.RedirectStandardInput = $true; $i.RedirectStandardOutput = $true; $i.RedirectStandardError = $true
    $p = New-Object System.Diagnostics.Process; $p.StartInfo = $i
    if (-not $p.Start()) { throw 'Celery probe could not start.' }
    $p.StandardInput.Write($Code); $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd(); $err = $p.StandardError.ReadToEnd(); $p.WaitForExit()
    [pscustomobject]@{ ExitCode = $p.ExitCode; Stdout = $out.Trim(); Stderr = $err.Trim() }
}
function Get-SafeDiagnostic([string]$Text) {
    if (-not $Text) { return '<empty>' }
    $safe = $Text -replace 'sk-[A-Za-z0-9_-]+', '<redacted-key>'
    $safe = $safe -replace '(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+', '$1<redacted>'
    $safe = $safe -replace '(?i)(://[^/@\s]*:)[^@\s]+@', '$1<redacted>@'
    if ($safe.Length -gt 400) { $safe = $safe.Substring(0, 400) }
    return $safe
}
function Test-HttpEndpoint([string]$Name, [string]$Url, [switch]$AcceptHttpError) {
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            Write-Host "$Name PASS|URL=$Url|HTTP=$($response.StatusCode)"
            return $true
        } catch {
            $lastError = $_.Exception.Message
            if ($AcceptHttpError -and $_.Exception.Response) {
                $status = [int]$_.Exception.Response.StatusCode
                Write-Host "$Name PASS|URL=$Url|HTTP=$status|ResponseReceived=YES"
                return $true
            }
            Start-Sleep -Seconds 1
        }
    }
    Write-Host "$Name FAIL|URL=$Url|ERROR=$(Get-SafeDiagnostic $lastError)"
    return $false
}
function Get-BackendListener {
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { [int]$_.LocalPort -eq 8000 })
    if ($listeners.Count -gt 0) {
        $script:lastListenerObservation = (($listeners | ForEach-Object {
            "ADDRESS=$([string]$_.LocalAddress)|PID=$([int]$_.OwningProcess)|STATE=$($_.State)"
        }) -join ';')
    } else {
        $script:lastListenerObservation = 'NO_LISTENER_ON_PORT_8000'
    }
    if ($listeners.Count -ne 1) { throw "Expected exactly one Backend listener on port 8000; found $($listeners.Count). Observation=$script:lastListenerObservation" }
    $listenerPid = [int]$listeners[0].OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid"
    if (-not $process) { throw "Backend listener PID $listenerPid disappeared during validation." }
    $address = [string]$listeners[0].LocalAddress
    Write-Host "LISTENER_PID=$listenerPid"
    Write-Host "LISTENER_EXECUTABLE=$($process.ExecutablePath)"
    Write-Host "LISTENER_COMMANDLINE=$(Get-SafeDiagnostic $process.CommandLine)"
    Write-Host "LISTENER_PARENT_PID=$($process.ParentProcessId)"
    $osPythonPass = [IO.Path]::GetFullPath($process.ExecutablePath) -ieq [IO.Path]::GetFullPath($python)
    Write-Host "OS_EXECUTABLE_DIAGNOSTIC=$(if ($osPythonPass) { 'MATCH' } else { 'DIFF_ALLOWED_FOR_WINDOWS_VENV' })"
    $addressPass = $address -in @('127.0.0.1', '0.0.0.0', '::1', '::', '::ffff:127.0.0.1')
    Write-Host "VALIDATION_ADDRESS=$(if ($addressPass) { 'PASS' } else { 'FAIL' })|ADDRESS=$address"
    if (-not $addressPass) { throw "Backend listener PID $listenerPid uses unexpected local address '$address'." }
    $appPass = ($process.CommandLine -match 'app\.main:app') -or ($process.CommandLine -match 'run_real_ai_backend\.py')
    Write-Host "VALIDATION_APP=$(if ($appPass) { 'PASS' } else { 'FAIL' })"
    if (-not $appPass) { throw "Backend listener PID $listenerPid is not running app.main:app." }
    return [pscustomobject]@{
        ProcessId = $process.ProcessId
        ExecutablePath = $process.ExecutablePath
        CommandLine = $process.CommandLine
        LocalAddress = $address
    }
}
function Get-BackendRuntime([int]$ListenerPid) {
    if (-not (Test-Path $runtimeDiagnosticPath -PathType Leaf)) {
        throw "Runtime diagnostic file is not available for listener PID $ListenerPid."
    }
    $runtime = Get-Content -LiteralPath $runtimeDiagnosticPath -Raw | ConvertFrom-Json
    $expectedPrefix = [IO.Path]::GetFullPath((Join-Path $root '.venv'))
    $expectedPython = [IO.Path]::GetFullPath($python)
    $expectedApp = [IO.Path]::GetFullPath((Join-Path $backend 'app\main.py'))
    Write-Host "RUNTIME_PID=$($runtime.pid)"
    Write-Host "RUNTIME_EXECUTABLE=$($runtime.sys_executable)"
    Write-Host "RUNTIME_PREFIX=$($runtime.sys_prefix)"
    Write-Host "RUNTIME_BASE_PREFIX=$($runtime.sys_base_prefix)"
    Write-Host "RUNTIME_APP_IMPORT=$($runtime.app_main)"
    $pidPass = [int]$runtime.pid -eq $ListenerPid
    $executablePass = [IO.Path]::GetFullPath([string]$runtime.sys_executable) -ieq $expectedPython
    $prefixPass = [IO.Path]::GetFullPath([string]$runtime.sys_prefix) -ieq $expectedPrefix
    $appPass = [IO.Path]::GetFullPath([string]$runtime.app_main) -ieq $expectedApp
    Write-Host "VALIDATION_RUNTIME_PID=$(if ($pidPass) { 'PASS' } else { 'FAIL' })"
    Write-Host "VALIDATION_RUNTIME_EXECUTABLE=$(if ($executablePass) { 'PASS' } else { 'FAIL' })"
    Write-Host "VALIDATION_RUNTIME_PREFIX=$(if ($prefixPass) { 'PASS' } else { 'FAIL' })"
    Write-Host "VALIDATION_RUNTIME_APP=$(if ($appPass) { 'PASS' } else { 'FAIL' })"
    if (-not ($pidPass -and $executablePass -and $prefixPass -and $appPass)) {
        throw "Backend listener runtime validation failed for PID $ListenerPid."
    }
    return $runtime
}
function Save-PreflightEvidence {
    $backendInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($b.Id)"
    $listenerInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.ProcessId)"
    $celeryInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($c.Id)"
    if (-not $backendInfo -or -not $listenerInfo -or -not $celeryInfo) {
        throw 'Cannot preserve preflight evidence because a validated process disappeared.'
    }
    $evidence = [ordered]@{
        schema_version = 1
        generated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        backend = [ordered]@{
            launch_pid = [int]$b.Id
            listener_pid = [int]$listener.ProcessId
            listener_parent_pid = [int]$listenerInfo.ParentProcessId
            launch_creation_time = [string]$backendInfo.CreationDate
            listener_creation_time = [string]$listenerInfo.CreationDate
            runtime_pid = [int]$runtime.pid
            sys_executable = [string]$runtime.sys_executable
            sys_prefix = [string]$runtime.sys_prefix
            sys_base_prefix = [string]$runtime.sys_base_prefix
            app_main = [string]$runtime.app_main
            listener_address = [string]$listener.LocalAddress
            os_executable = [string]$listenerInfo.ExecutablePath
            command_line = Get-SafeDiagnostic $listenerInfo.CommandLine
        }
        celery = [ordered]@{
            pid = [int]$c.Id
            parent_pid = [int]$celeryInfo.ParentProcessId
            creation_time = [string]$celeryInfo.CreationDate
            executable = [string]$celeryInfo.ExecutablePath
            queue = $queue
            worker = $worker
            redis_db = 14
        }
        database = [ordered]@{ name = 'vora_test'; port = 55432; schema_check = 'PASS' }
        storage = $storage
        rules_version = [ordered]@{ id = $rulesVersionId; slide_count = 5 }
        provider = [ordered]@{
            fake_providers = $false
            max_retries = 0
            image_model = 'gpt-image-2.5-sunburst'
            text_models = @('gpt-4o-mini')
            image_quality = 'low'
            image_size = '1024x1024'
            image_count = 1
        }
        workflow = [ordered]@{
            expected_stop = 'HUMAN_APPROVAL'
            publication_auto_run = $false
            api_calls_at_start = 0
        }
    }
    New-Item -ItemType Directory -Force $evidenceRoot | Out-Null
    Copy-Item -LiteralPath $runtimeDiagnosticPath -Destination $evidenceRuntimePath -Force
    $evidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $evidencePath -Encoding UTF8
    Write-Host "Preflight evidence: $evidencePath"
}

try {
    # An empty list is the safe, expected state after the dedicated E2E stop
    # script completed. If IDs are supplied, validate them before any start.
    if (-not (Test-Path $python -PathType Leaf)) { throw "Python not found: $python" }
    if (-not $env:VORA_TEST_POSTGRES_URL) { throw 'VORA_TEST_POSTGRES_URL is missing.' }
    $u = [Uri]$env:VORA_TEST_POSTGRES_URL
    if ($u.Port -ne 55432 -or $u.AbsolutePath.TrimStart('/') -ne 'vora_test') { throw 'Dedicated vora_test:55432 DB required.' }
    $ui = $u.UserInfo.Split(':', 2)
    if ($ui.Count -ne 2) { throw 'Invalid PostgreSQL URL user information.' }
    $env:DB_HOST=$u.Host; $env:DB_PORT=[string]$u.Port; $env:DB_NAME=$u.AbsolutePath.TrimStart('/')
    $env:DB_USER=[Uri]::UnescapeDataString($ui[0]); $env:DB_PASSWORD=[Uri]::UnescapeDataString($ui[1])

    $dbCode = @'
import os
import psycopg
with psycopg.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]), dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], connect_timeout=5) as c:
    with c.cursor() as cur:
        cur.execute("SELECT current_database(), to_regclass('public.alembic_version')")
        db, table = cur.fetchone()
        if db != "vora_test" or table != "alembic_version": raise SystemExit("DB schema check failed")
        cur.execute("SELECT version_num FROM alembic_version")
        revision = cur.fetchone()[0]
        cur.execute("SELECT id, rules_data->>'slide_count' FROM rules_versions WHERE id = 7291")
        rules = cur.fetchone()
        if rules is None or rules[1] != "5": raise SystemExit("RulesVersion 7291 must define exactly five slides")
        print(f"DB_SCHEMA=PASS|REVISION={revision}|RULES_VERSION=7291|SLIDE_COUNT=5")
'@
    Invoke-PythonStdin 'PostgreSQL schema' $dbCode

    $env:REDIS_URL='redis://localhost:6379/14'; $env:GENERATION_QUEUE=$queue
    $env:PUBLICATION_QUEUE=$queue + '_publication'; $env:VIDEO_GENERATION_QUEUE=$queue + '_video'
    $redisCode = @'
import os
import redis
r = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5)
if not r.ping(): raise SystemExit("Redis ping failed")
qs = [os.environ["GENERATION_QUEUE"], os.environ["PUBLICATION_QUEUE"], os.environ["VIDEO_GENERATION_QUEUE"]]
if any(r.llen(q) for q in qs): raise SystemExit("Dedicated Redis queues are not empty")
print(f"REDIS_DB=14|DBSIZE={r.dbsize()}|QUEUES_EMPTY=PASS")
'@
    Invoke-PythonStdin 'Redis DB 14' $redisCode

    if (Test-Path $storage -PathType Leaf) { throw "Storage path is a file: $storage" }
    New-Item -ItemType Directory -Force $storage | Out-Null
    $secure = Read-Host 'OpenAI API key' -AsSecureString
    $keyPtr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $env:OPENAI_API_KEY=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr)
    $env:UPLOADS_ROOT=$storage; $env:OPENAI_MAX_RETRIES='0'; $env:OPENAI_IMAGE_QUALITY='low'
    $env:OPENAI_IMAGE_SIZE='1024x1024'; $env:OPENAI_IMAGE_COUNT='1'; $env:VORA_E2E_FAKE_PROVIDERS='false'
    $env:IMAGE_GENERATION_MODEL='gpt-image-2.5-sunburst'; $env:CONTENT_PLANNING_MODEL='gpt-4o-mini'; $env:SCRIPT_GENERATION_MODEL='gpt-4o-mini'
    $env:VIRTUAL_ENV=(Join-Path $root '.venv'); Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:PYTHONPATH=$backend; $env:PATH="$(Join-Path $root '.venv\Scripts');$env:PATH"

    $runtimeCode = @'
import sys
import app
print(f"PYTHON_EXECUTABLE={sys.executable}")
print(f"PYTHON_PREFIX={sys.prefix}")
print(f"PYTHON_BASE_PREFIX={sys.base_prefix}")
print(f"APP_IMPORT={app.__file__}")
'@
    Invoke-PythonStdin 'Backend runtime' $runtimeCode

    $current=@(Get-CimInstance Win32_Process | Where-Object {$ExpectedProcessIds -contains $_.ProcessId})
    $backendPattern = 'run_real_ai_backend\.py'
    $celeryPattern = '-m celery .*app\.celery_app.*--queues ' + [Regex]::Escape($queue) + ' .*--hostname ' + [Regex]::Escape($worker)
    foreach($p in $current){
        $isBackend = $p.CommandLine -match $backendPattern
        $isCelery = $p.CommandLine -match $celeryPattern
        if (-not ($isBackend -or $isCelery)) {
            throw "Expected PID $($p.ProcessId) is not a recognized VORA Backend/Celery process; aborting without termination."
        }
    }
    foreach($l in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue|Where-Object {$_.LocalPort -eq 8000})){if($ExpectedProcessIds -notcontains $l.OwningProcess){throw "Unknown process owns port 8000: PID $($l.OwningProcess)"}}
    foreach($p in $current){Stop-Process $p.ProcessId -Force}; Start-Sleep 2
    New-Item -ItemType Directory -Force $diagnosticDir | Out-Null
    $backendStdout = Join-Path $diagnosticDir 'backend.stdout.log'; $backendStderr = Join-Path $diagnosticDir 'backend.stderr.log'
    $celeryStdout = Join-Path $diagnosticDir 'celery.stdout.log'; $celeryStderr = Join-Path $diagnosticDir 'celery.stderr.log'
    $diagnosticFiles = @($backendStdout, $backendStderr, $celeryStdout, $celeryStderr)
    $runtimeDiagnosticPath = Join-Path $diagnosticDir 'backend.runtime.json'
    $env:VORA_RUNTIME_DIAGNOSTIC_PATH = $runtimeDiagnosticPath
    $b=Start-Process $python -ArgumentList @($backendLauncher) -WorkingDirectory $backend -PassThru -WindowStyle Hidden -RedirectStandardOutput $backendStdout -RedirectStandardError $backendStderr; $started+=$b
    $c=Start-Process $python -ArgumentList @('-m','celery','-A','app.celery_app','worker','--loglevel=INFO','--queues',$queue,'--pool=solo','--hostname',$worker) -WorkingDirectory $backend -PassThru -WindowStyle Hidden -RedirectStandardOutput $celeryStdout -RedirectStandardError $celeryStderr; $started+=$c
    $listener = $null; $listenerValidationError = $null; $runtime = $null
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        foreach($p in @($b,$c)){if($p.HasExited){throw "New process exited early: PID $($p.Id)"}}
        try {
            $listener = Get-BackendListener
            $runtime = Get-BackendRuntime ([int]$listener.ProcessId)
            break
        } catch { $listenerValidationError = Get-SafeDiagnostic $_.Exception.Message; Start-Sleep -Seconds 1 }
    }
    if ($null -eq $listener) { throw "Backend listener validation failed after 15 seconds. Observation=$lastListenerObservation; LastValidation=$listenerValidationError" }
    Write-Host "Backend listener: PASS|PID=$($listener.ProcessId)|ADDRESS=$([string]$listener.LocalAddress)|EXECUTABLE=$($listener.ExecutablePath)"
    if (-not (Test-HttpEndpoint 'Backend health' 'http://127.0.0.1:8000/docs')) { throw 'Backend health failed.' }
    $env:VORA_E2E_WORKER_NAME = $worker
    $probeCode = @'
import os
from app.celery_app import celery_app
worker_name = os.environ["VORA_E2E_WORKER_NAME"]
expected_queue = os.environ["GENERATION_QUEUE"]
responses = celery_app.control.inspect([worker_name]).active_queues() or {}
worker_queues = responses.get(worker_name)
if worker_queues is None:
    print("NO_WORKER_RESPONSE")
    raise SystemExit(2)
queue_names = [item.get("name") for item in worker_queues]
print("WORKER=" + worker_name + "|QUEUES=" + ",".join(queue_names))
if set(queue_names) != {expected_queue}:
    raise SystemExit(3)
'@
    $ready = $false; $lastProbe = $null
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        if ($c.HasExited) { throw "Celery worker exited before readiness: PID $($c.Id)" }
        $lastProbe = Invoke-CeleryProbe $probeCode
        if ($lastProbe.ExitCode -eq 0 -and $lastProbe.Stdout -match [Regex]::Escape($worker) -and $lastProbe.Stdout -match [Regex]::Escape($queue)) { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        Write-Host 'Celery readiness failed after 12 attempts.'
        if ($lastProbe) {
            Write-Host "Celery probe stdout: $(Get-SafeDiagnostic $lastProbe.Stdout)"
            Write-Host "Celery probe stderr: $(Get-SafeDiagnostic $lastProbe.Stderr)"
            Write-Host "Celery probe exit: $($lastProbe.ExitCode)"
        }
        throw 'Celery dedicated queue readiness failed.'
    }
    Write-Host "Celery readiness: PASS|WORKER=$worker|QUEUE=$queue"
    $frontendReady = Test-HttpEndpoint 'Frontend health' 'http://127.0.0.1:5173/'
    if (-not $frontendReady) {
        Write-Host 'Backend/Celery remain running; only Frontend readiness failed.'
        Write-Host "Backend PID: $($b.Id)"
        Write-Host "Celery PID: $($c.Id)"
        Write-Host "Start Frontend with: Set-Location '$root\frontend'; npm.cmd run dev -- --host 127.0.0.1"
        exit 2
    }
    $proxyReady = Test-HttpEndpoint 'Frontend proxy' 'http://127.0.0.1:5173/workflow-executions' -AcceptHttpError
    if (-not $proxyReady) {
        Write-Host 'Frontend is reachable, but its Backend proxy endpoint could not be reached.'
        Write-Host "Backend PID: $($b.Id)"
        Write-Host "Celery PID: $($c.Id)"
        exit 2
    }
    Save-PreflightEvidence
    Write-Host '[PASS] Isolated environment ready; API calls: 0'; Write-Host "Backend PID: $($b.Id)"; Write-Host "Backend listener PID: $($listener.ProcessId)"; Write-Host "Celery PID: $($c.Id)"
    Write-Host "Stop: .\scripts\stop-real-ai-e2e.ps1 -BackendPid $($b.Id) -CeleryPid $($c.Id)"
} catch { Stop-Started; Read-ProcessDiagnostics; Write-Error $_.Exception.Message; exit 1 } finally { Clear-Secrets; Clear-ProcessDiagnostics }
