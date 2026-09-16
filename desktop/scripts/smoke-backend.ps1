$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$desktopRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$projectRoot = Resolve-Path (Join-Path $desktopRoot "..")
$exe = Join-Path $projectRoot "backend\dist\aegis-backend\aegis-backend.exe"
$testRoot = Join-Path $env:TEMP "aegiscopilot-desktop-smoke"
$port = 18123

if (-not (Test-Path $exe)) {
    throw "Packaged backend executable was not found: $exe"
}

New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$env:AEGIS_ENV = "test"
$env:AEGIS_LLM_API_KEY = ""
$env:AEGIS_STORAGE_DIR = Join-Path $testRoot "storage"
$env:AEGIS_KNOWLEDGE_DIR = Join-Path $projectRoot "knowledge"
$env:AEGIS_MODEL_DIR = Join-Path $projectRoot "models\cache"
$env:AEGIS_EMBEDDING_ENABLED = "1"
$env:AEGIS_EMBEDDING_LOCAL_ONLY = "1"
$env:AEGIS_ALLOWED_ORIGINS = "aegis://app"

$stdoutPath = Join-Path $testRoot "backend.out.log"
$stderrPath = Join-Path $testRoot "backend.err.log"
$process = Start-Process `
    -FilePath $exe `
    -ArgumentList @("--host", "127.0.0.1", "--port", "$port") `
    -WorkingDirectory (Split-Path $exe) `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($process.HasExited) {
            Get-Content $stderrPath -ErrorAction SilentlyContinue
            throw "Packaged backend exited before becoming ready (code $($process.ExitCode))."
        }
        try {
            $health = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$port/health" -TimeoutSec 2
            $ready = $true
            break
        } catch {
            Start-Sleep -Seconds 1
        }
    }

    if (-not $ready) {
        Get-Content $stderrPath -ErrorAction SilentlyContinue
        throw "Packaged backend did not become ready."
    }

    if ($health.version -ne "2.4.0") {
        throw "Unexpected backend version: $($health.version)"
    }

    $profile = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$port/profile" -TimeoutSec 5
    if ($null -eq $profile.personalization_enabled -or $null -eq $profile.auto_memory_enabled) {
        throw "Packaged backend did not expose the v2.4 profile contract."
    }

    $memoryBody = @{
        scope = "global"
        knowledge_base_id = $null
        category = "response_style"
        key = "smoke.language"
        value = "en"
        display_text = "Answer in English"
        pinned = $false
    } | ConvertTo-Json -Compress
    $memoryBytes = [System.Text.Encoding]::UTF8.GetBytes($memoryBody)
    $memory = Invoke-RestMethod `
        -UseBasicParsing `
        -Uri "http://127.0.0.1:$port/profile/memories" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $memoryBytes `
        -TimeoutSec 5
    if (-not $memory.id -or $memory.scope -ne "global" -or $memory.category -ne "response_style") {
        throw "Packaged backend did not persist the v2.4 memory contract."
    }
    $memoryList = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$port/profile/memories?ids=$($memory.id)" -TimeoutSec 5
    if ($memoryList.items.Count -ne 1 -or $memoryList.items[0].id -ne $memory.id) {
        throw "Packaged backend did not read the persisted v2.4 memory."
    }
    Invoke-RestMethod `
        -UseBasicParsing `
        -Uri "http://127.0.0.1:$port/profile/memories/$($memory.id)" `
        -Method Delete `
        -TimeoutSec 5 | Out-Null

    $knowledge = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$port/knowledge/status" -TimeoutSec 5
    if (-not $knowledge.ready -or $knowledge.topic_count -ne 300) {
        throw "Packaged backend did not load the 300-topic knowledge base."
    }

    for ($attempt = 0; $attempt -lt 60 -and $knowledge.retrieval_mode -ne "hybrid" -and -not $knowledge.embedding_error; $attempt++) {
        Start-Sleep -Seconds 1
        $knowledge = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$port/knowledge/status" -TimeoutSec 5
    }
    if ($knowledge.retrieval_mode -ne "hybrid") {
        throw "Packaged vector retrieval did not initialize: $($knowledge.embedding_error)"
    }

    # Keep the smoke payload ASCII-only so Windows PowerShell 5.1 source encoding
    # cannot corrupt it before UTF-8 request serialization.
    $body = @{ query = "git revert previous commit safely" } | ConvertTo-Json -Compress
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $answer = Invoke-WebRequest `
        -UseBasicParsing `
        -Uri "http://127.0.0.1:$port/chat/stream" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bodyBytes `
        -TimeoutSec 30
    if ($answer.Content -notmatch "git revert" -or $answer.Content -notmatch "event: done") {
        $answer.Content
        throw "Packaged backend did not return the expected offline SSE answer."
    }

    @{
        health = $health
        knowledge = @{
            ready = $knowledge.ready
            topic_count = $knowledge.topic_count
            retrieval_mode = $knowledge.retrieval_mode
            embedding_model = $knowledge.embedding_model
        }
        profile = $true
        memory_crud = $true
        offline_answer = $true
    } | ConvertTo-Json -Compress -Depth 4
} finally {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
}
