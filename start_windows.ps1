# Right-click > Run with PowerShell, or run:  powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
# Needs Python 3.10, 3.11 or 3.12. (3.13 / 3.14 are too new for the OCR package rapidocr-onnxruntime.)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    foreach ($v in "3.12", "3.11", "3.10") {
        try { $null = & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return $v } } catch {}
    }
    return $null
}

# Rebuild a venv that was created with an unsupported Python (e.g. 3.14 from an earlier attempt)
if (Test-Path .\.venv\Scripts\python.exe) {
    $venvVer = & .\.venv\Scripts\python.exe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    if ($venvVer -notin @("3.10", "3.11", "3.12")) {
        Write-Host "Existing .venv uses Python $venvVer (not supported). Removing it..."
        Remove-Item -Recurse -Force .\.venv
    }
}

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    $pyv = Find-Python
    if (-not $pyv) {
        Write-Host ""
        Write-Host "Python 3.12 was not found. Install it, then run this script again:" -ForegroundColor Yellow
        Write-Host "  winget install -e --id Python.Python.3.12" -ForegroundColor Yellow
        Write-Host "  (or download 3.12 from https://www.python.org/downloads/windows/)" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "First run: creating virtual environment with Python $pyv and installing packages (5-10 minutes)..."
    & py "-$pyv" -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "Package install failed - see the error above." -ForegroundColor Red; Remove-Item -Recurse -Force .\.venv; exit 1 }
    .\.venv\Scripts\python.exe -m spacy download en_core_web_sm
    if ($LASTEXITCODE -ne 0) { Write-Host "spaCy model download failed - name detection will be switched off." -ForegroundColor Yellow }
}

# Start the local OHIF viewer (http://localhost:3000/local) in the background, if its files are present
$ohif = $null
if (Test-Path .\ohif-local\viewer\index.html) {
    $ohif = Start-Process -FilePath .\.venv\Scripts\python.exe -ArgumentList "serve_ohif.py", "--no-browser" -WindowStyle Hidden -PassThru
    Write-Host "OHIF viewer: http://localhost:3000/local"
} else {
    Write-Host "OHIF viewer not installed (extract ohif-local-viewer-part1/2.zip here to enable it)." -ForegroundColor Yellow
}
try {
    .\.venv\Scripts\python.exe -m app.server
} finally {
    if ($ohif -and -not $ohif.HasExited) { Stop-Process -Id $ohif.Id -Force; Write-Host "OHIF viewer stopped." }
}
