param([string]$Python = 'python', [string]$BuildDependencies = '')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$oldPythonPath = $env:PYTHONPATH
try {
    if ($BuildDependencies) { $env:PYTHONPATH = (Resolve-Path -LiteralPath $BuildDependencies).Path }
    Push-Location -LiteralPath $projectRoot
    try {
        & $Python -m PyInstaller packaging/violet.spec --noconfirm --distpath dist --workpath .build/pyinstaller-0.8.1
        if ($LASTEXITCODE -ne 0) { throw 'Violet EXE build failed' }
        Write-Output (Join-Path $projectRoot 'dist/Violet.exe')
    } finally { Pop-Location }
} finally { $env:PYTHONPATH = $oldPythonPath }
