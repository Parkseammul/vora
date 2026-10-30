[CmdletBinding()]
param(
    [int[]]$ExpectedProcessIds = @()
)

& (Join-Path $PSScriptRoot 'start-real-ai-e2e.impl.ps1') -ExpectedProcessIds $ExpectedProcessIds
exit $LASTEXITCODE

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $repoRoot 'backend'
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$storagePath = Join-Path $backendRoot 'uploads\real-ai-e2e-20261030'
$generationQueue = 'vora_real_ai_e2e_20261030'
$workerName = 'vora-real-ai-e2e@localhost'
$startedProcesses = New-Object 'System.Collections.Generic.List[System.Diagnostics.Process]'
$apiKeyPointer = $null

function Clear-ParentSecrets {
    if ($null -ne $script:apiKeyPointer) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($script:apiKeyPointer)
        $script:apiKeyPointer = $null
    }
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:DB_PASSWORD -ErrorAction SilentlyContinue
}

function Stop-StartedProcesses {
    foreach ($process in @($script:startedProcesses)) {
        try {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {
            Write-Warning "새로 시작한 PID $($process.Id)의 정리에 실패했습니다."
        }
    }
}

function Invoke-VenvPython {
    param([string[]]$Arguments)
    & $pythonPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 사전검증 실패: $($Arguments -join ' ')"
    }
}

try {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "프로젝트 .venv Python을 찾을 수 없습니다: $pythonPath"
    }
    if (-not (Test-Path -LiteralPath $backendRoot -PathType Container)) {
        throw "Backend 디렉터리를 찾을 수 없습니다."
    }

    Push-Location -LiteralPath $backendRoot
    try {
        $pythonVersion = & $pythonPath -c 'import sys; print(sys.version.split()[0]); raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
        if ($LASTEXITCODE -ne 0) {
            throw ".venv Python은 Python 3.12이어야 합니다."
        }
        Write-Host "[PASS] Python: $pythonVersion"
    } finally {
        Pop-Location
    }

    if (-not $env:VORA_TEST_POSTGRES_URL) {
        throw 'VORA_TEST_POSTGRES_URL이 없어 전용 PostgreSQL 연결을 확인할 수 없습니다.'
    }
    $dbUri = [Uri]$env:VORA_TEST_POSTGRES_URL
    if ($dbUri.Port -ne 55432 -or $dbUri.AbsolutePath.TrimStart('/') -ne 'vora_test') {
        throw 'VORA_TEST_POSTGRES_URL이 전용 vora_test:55432 DB를 가리키지 않습니다.'
    }
    $dbUserInfo = $dbUri.UserInfo.Split(':', 2)
    if ($dbUserInfo.Count -ne 2) {
        throw '전용 PostgreSQL URL의 사용자 정보가 올바르지 않습니다.'
    }
    $env:DB_HOST = $dbUri.Host
    $env:DB_PORT = [string]$dbUri.Port
    $env:DB_NAME = $dbUri.AbsolutePath.TrimStart('/')
    $env:DB_USER = [Uri]::UnescapeDataString($dbUserInfo[0])
    $env:DB_PASSWORD = [Uri]::UnescapeDataString($dbUserInfo[1])

    Invoke-VenvPython @('-c', @'
import os
import psycopg

with psycopg.connect(
    host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"], connect_timeout=5,
) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), to_regclass('public.alembic_version')")
        database, alembic_table = cur.fetchone()
        if database != "vora_test" or alembic_table != "alembic_version":
            raise SystemExit("전용 DB 또는 Alembic schema가 아닙니다.")
        cur.execute("SELECT version_num FROM alembic_version")
        revision = cur.fetchone()[0]
        print(f"DB_SCHEMA=PASS|REVISION={revision}")
'@)

    $env:REDIS_URL = 'redis://localhost:6379/14'
    $env:GENERATION_QUEUE = $generationQueue
    $env:PUBLICATION_QUEUE = "$generationQueue`_publication"
    $env:VIDEO_GENERATION_QUEUE = "$generationQueue`_video"
    Invoke-VenvPython @('-c', @'
import os
import redis

client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5)
if not client.ping():
    raise SystemExit("Redis ping failed")
queues = [
    os.environ["GENERATION_QUEUE"],
    os.environ["PUBLICATION_QUEUE"],
    os.environ["VIDEO_GENERATION_QUEUE"],
]
lengths = {queue: client.llen(queue) for queue in queues}
if any(lengths.values()):
    raise SystemExit(f"전용 Redis queue에 기존 작업이 남아 있습니다: {lengths}")
print(f"REDIS_DB=14|DBSIZE={client.dbsize()}|QUEUES_EMPTY=PASS")
'@)

    if (Test-Path -LiteralPath $storagePath -PathType Leaf) {
        throw "Storage 경로가 파일로 존재합니다: $storagePath"
    }
    New-Item -ItemType Directory -Force -Path $storagePath | Out-Null
    Write-Host "[PASS] Storage: $storagePath"

    $secureKey = Read-Host 'OpenAI API key' -AsSecureString
    $apiKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPointer)

    $env:UPLOADS_ROOT = $storagePath
    $env:OPENAI_MAX_RETRIES = '0'
    $env:OPENAI_IMAGE_QUALITY = 'low'
    $env:OPENAI_IMAGE_SIZE = '1024x1024'
    $env:OPENAI_IMAGE_COUNT = '1'
    $env:VORA_E2E_FAKE_PROVIDERS = 'false'
    $env:IMAGE_GENERATION_MODEL = 'gpt-image-2.5-sunburst'
    $env:CONTENT_PLANNING_MODEL = 'gpt-4o-mini'
    $env:SCRIPT_GENERATION_MODEL = 'gpt-4o-mini'

    # 모든 사전검증이 끝난 뒤에만 기존 프로세스의 정확한 PID를 검증한다.
    $current = @(Get-CimInstance Win32_Process | Where-Object {
        $ExpectedProcessIds -contains $_.ProcessId
    })
    $expectedPatterns = @{
        10868 = '\.venv\\Scripts\\python\.exe.*-m celery .*--queues generation .*--hostname=vora-dev-generation@localhost'
        33784 = '\.venv\\Scripts\\python\.exe.*-m uvicorn app\.main:app .*--port 8000'
        19132 = 'Python312\\python\.exe.*-m celery .*--queues generation .*--hostname=vora-dev-generation@localhost'
        28736 = 'Python312\\python\.exe.*-m uvicorn app\.main:app .*--port 8000'
    }
    foreach ($process in $current) {
        if (-not $expectedPatterns.ContainsKey([int]$process.ProcessId) -or
            $process.CommandLine -notmatch $expectedPatterns[[int]$process.ProcessId]) {
            throw "기대 PID $($process.ProcessId)가 다른 프로세스로 재사용되어 기존 프로세스 종료를 중단합니다."
        }
    }

    $port8000 = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq 8000 })
    foreach ($listener in $port8000) {
        if ($ExpectedProcessIds -notcontains $listener.OwningProcess) {
            throw "8000 포트에 불명확한 프로세스가 있어 중단합니다: PID $($listener.OwningProcess)"
        }
    }

    foreach ($process in $current) {
        Stop-Process -Id $process.ProcessId -Force
    }
    Start-Sleep -Seconds 2

    $backendProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $backendRoot -PassThru -WindowStyle Hidden
    $startedProcesses.Add($backendProcess)

    $celeryProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @('-m', 'celery', '-A', 'app.celery_app', 'worker', '--loglevel=INFO', '--queues', $generationQueue, '--pool=solo', '--hostname', $workerName) `
        -WorkingDirectory $backendRoot -PassThru -WindowStyle Hidden
    $startedProcesses.Add($celeryProcess)

    Start-Sleep -Seconds 5
    foreach ($process in @($backendProcess, $celeryProcess)) {
        if ($process.HasExited) {
            throw "새로 시작한 프로세스가 조기 종료되었습니다: PID $($process.Id)"
        }
    }

    $health = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/docs' -UseBasicParsing -TimeoutSec 10
    if ($health.StatusCode -ne 200) {
        throw "Backend health check 실패: HTTP $($health.StatusCode)"
    }

    Push-Location -LiteralPath $backendRoot
    try {
        $inspectOutput = & $pythonPath -m celery -A app.celery_app inspect active_queues -d $workerName --timeout=5 2>$null
        if ($LASTEXITCODE -ne 0 -or ($inspectOutput -notmatch [Regex]::Escape($workerName))) {
            throw 'Celery 전용 worker 식별에 실패했습니다.'
        }
        if ($inspectOutput -notmatch [Regex]::Escape($generationQueue)) {
            throw 'Celery worker가 전용 generation queue를 소비하지 않습니다.'
        }
    } finally {
        Pop-Location
    }

    $frontend = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/' -UseBasicParsing -TimeoutSec 10
    if ($frontend.StatusCode -ne 200) {
        throw "Frontend health check 실패: HTTP $($frontend.StatusCode)"
    }

    Write-Host ''
    Write-Host '[PASS] VORA isolated environment is ready; no API request was made.'
    Write-Host "Backend PID: $($backendProcess.Id)"
    Write-Host "Celery PID: $($celeryProcess.Id)"
    Write-Host 'Provider: REAL'
    Write-Host 'Image model: gpt-image-2.5-sunburst'
    Write-Host 'Image settings: quality=low, size=1024x1024, count=1, max_retries=0'
    Write-Host "Generation queue: $generationQueue"
    Write-Host 'Redis DB: 14'
    Write-Host 'API calls executed: 0'
    Write-Host "Stop command: .\scripts\stop-real-ai-e2e.ps1 -BackendPid $($backendProcess.Id) -CeleryPid $($celeryProcess.Id)"
}
catch {
    Stop-StartedProcesses
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    Clear-ParentSecrets
}
