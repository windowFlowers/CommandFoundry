$ErrorActionPreference = "Stop"

$desktopRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$projectRoot = Resolve-Path (Join-Path $desktopRoot "..")
$backendRoot = Join-Path $projectRoot "backend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$entrypoint = Join-Path $backendRoot "desktop_entry.py"
$hooksRoot = Join-Path $desktopRoot "pyinstaller-hooks"
$distRoot = Join-Path $backendRoot "dist"
$workRoot = Join-Path $backendRoot "build"

if (-not (Test-Path $python)) {
    throw "Backend virtualenv was not found: $python"
}
if (-not (Test-Path $entrypoint)) {
    throw "Backend desktop entrypoint was not found: $entrypoint"
}

New-Item -ItemType Directory -Force -Path $distRoot,$workRoot | Out-Null

Push-Location $projectRoot
try {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --name aegis-backend `
        --paths $backendRoot `
        --distpath $distRoot `
        --workpath (Join-Path $workRoot "pyinstaller") `
        --specpath (Join-Path $workRoot "spec") `
        --additional-hooks-dir $hooksRoot `
        --collect-all fastembed `
        --collect-all onnxruntime `
        --hidden-import rank_bm25 `
        $entrypoint
    if ($LASTEXITCODE -ne 0) {
        throw "Backend desktop build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
