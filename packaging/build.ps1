# Builds dist/realtime-ttt.exe - a single file with Python bundled in.
#
#   .\.venv\Scripts\python -m pip install -r packaging\requirements-packaging.txt
#   .\packaging\build.ps1
#
# Run from the repository root.

$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No virtualenv at $python - see README." }

# --add-data source paths are resolved relative to --specpath, not the working
# directory, so an absolute path is the only thing that behaves the same wherever
# the spec is written.
$root = (Resolve-Path .).Path
$static = Join-Path $root "static"

# static/ is bundled as data because it is read at runtime; app/main.py resolves
# it through sys._MEIPASS when frozen.
#
# collect-all is not belt and braces here: uvicorn imports its protocol
# implementations by name at runtime, so a static analysis of the imports misses
# them entirely and the exe dies on the first request instead of at build time.
& $python -m PyInstaller `
  --onefile `
  --name realtime-ttt `
  --distpath dist `
  --workpath build\pyinstaller `
  --specpath build `
  --noconfirm `
  --add-data "$static;static" `
  --collect-all uvicorn `
  --collect-all websockets `
  --collect-all segno `
  --hidden-import httptools `
  --paths . `
  packaging\launcher.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with $LASTEXITCODE" }

Write-Host ""
Write-Host "Built dist\realtime-ttt.exe" -ForegroundColor Green
Write-Host "The database is written next to the exe, in dist\data\games.db"
