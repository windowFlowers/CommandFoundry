$ErrorActionPreference = "Stop"

$desktopRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$projectRoot = Resolve-Path (Join-Path $desktopRoot "..")
$frontendRoot = Join-Path $projectRoot "frontend"

if (-not (Test-Path (Join-Path $frontendRoot "node_modules"))) {
    throw "Frontend dependencies are missing. Run npm.cmd install in $frontendRoot first."
}

Push-Location $frontendRoot
try {
    npm.cmd run build -- --base=./
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend desktop build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
