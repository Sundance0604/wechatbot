param([string]$Python = "python", [switch]$OneFile)
$ErrorActionPreference = "Stop"
$cleanerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location (Join-Path $cleanerRoot "frontend")
try {
    & npm.cmd run build:cleaner
    if ($LASTEXITCODE -ne 0) { throw "React build failed" }
} finally { Pop-Location }
Push-Location $cleanerRoot
$previousMode = $env:CLEANER_ONEFILE
try {
    if ($OneFile) { $env:CLEANER_ONEFILE = "1" } else { $env:CLEANER_ONEFILE = "0" }
    & $Python -m PyInstaller packaging/cleaner.spec --noconfirm --distpath dist --workpath .build/pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Executable build failed" }
    if ($OneFile) { Write-Output (Join-Path $cleanerRoot "dist/WeChatCleaner.exe") }
    else { Write-Output (Join-Path $cleanerRoot "dist/WeChatCleaner/WeChatCleaner.exe") }
} finally { $env:CLEANER_ONEFILE = $previousMode; Pop-Location }
