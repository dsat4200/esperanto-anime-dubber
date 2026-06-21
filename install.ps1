$ErrorActionPreference = "Stop"

function Install-Ffmpeg {
    $ffmpegDir = ".\ffmpeg"
    if (Test-Path "$ffmpegDir\bin\ffmpeg.exe") {
        Write-Host "  ffmpeg already installed at $ffmpegDir\bin\" -ForegroundColor Green
        return $true
    }

    Write-Host "  Downloading ffmpeg..." -ForegroundColor Yellow

    try {
        $headers = @{ "User-Agent" = "omnivoice-installer" }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest" -Headers $headers
        $asset = $release.assets | Where-Object {
            $_.name -like "*win64-gpl.zip" -and $_.name -notlike "*shared*"
        } | Select-Object -First 1

        if (-not $asset) {
            throw "No suitable ffmpeg build found in latest release"
        }

        $zipPath = Join-Path $env:TEMP "ffmpeg-omnivoice.zip"
        Write-Host "  Downloading $($asset.name)..." -ForegroundColor DarkGray
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing

        if (Test-Path $ffmpegDir) { Remove-Item $ffmpegDir -Recurse -Force }
        Expand-Archive -Path $zipPath -DestinationPath $ffmpegDir -Force

        $subDir = Get-ChildItem $ffmpegDir -Directory | Select-Object -First 1
        if ($subDir) {
            Get-ChildItem $subDir.FullName -Force | ForEach-Object {
                Move-Item -LiteralPath $_.FullName -Destination $ffmpegDir -Force
            }
            Remove-Item -LiteralPath $subDir.FullName -Force -Recurse
        }
        Remove-Item $zipPath -Force

        if (Test-Path "$ffmpegDir\bin\ffmpeg.exe") {
            Write-Host "  ffmpeg installed to $ffmpegDir\bin\" -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "  Auto-download failed: $_" -ForegroundColor Red
    }

    Write-Host "  Please install ffmpeg manually from https://github.com/yt-dlp/FFmpeg-Builds/releases" -ForegroundColor Yellow
    Write-Host "  Extract to .\ffmpeg\ so that .\ffmpeg\bin\ffmpeg.exe exists." -ForegroundColor Yellow
    return $false
}

Write-Host ""
Write-Host "=== omnivoice installer ===" -ForegroundColor Cyan
Write-Host ""

$pyVersion = & python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Python not found. Install Python 3.10+ from https://python.org" -ForegroundColor Red
    exit 1
}
Write-Host "Found: $pyVersion" -ForegroundColor Green

$venvPath = ".venv"
$venvPython = "$venvPath\Scripts\python.exe"
$venvActivate = "$venvPath\Scripts\Activate.ps1"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment (without pip)..." -ForegroundColor Yellow
    & python -m venv --without-pip $venvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Write-Host "Error: Virtual environment creation failed." -ForegroundColor Red
        exit 1
    }
} elseif (-not (Test-Path "$venvPath\Scripts\pip.exe")) {
    Write-Host "Found venv without pip, bootstrapping pip..." -ForegroundColor Yellow
}

if (-not (Test-Path "$venvPath\Scripts\pip.exe")) {
    Write-Host "Bootstrapping pip via get-pip.py..." -ForegroundColor Yellow
    $getPip = Join-Path $env:TEMP "get-pip.py"
    try {
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing -TimeoutSec 60
    } catch {
        Write-Host "Error: Failed to download get-pip.py: $_" -ForegroundColor Red
        exit 1
    }
    & $venvPython $getPip
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: get-pip.py failed." -ForegroundColor Red
        exit 1
    }
    Remove-Item $getPip -Force -ErrorAction SilentlyContinue
}

Write-Host "Upgrading pip..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: pip upgrade failed." -ForegroundColor Red
    exit 1
}

Write-Host "Activating virtual environment..." -ForegroundColor Yellow
& $venvActivate

Write-Host "Installing PyTorch (CUDA 12.8 for Blackwell RTX 50-series)..." -ForegroundColor Yellow
& pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: PyTorch installation failed." -ForegroundColor Red
    exit 1
}

Write-Host "Installing dependencies (demucs, omnivoice, rich, soundfile)..." -ForegroundColor Yellow
& pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Dependency installation failed." -ForegroundColor Red
    exit 1
}



Write-Host "Installing anidub (project)..." -ForegroundColor Yellow
& pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: anidub installation failed." -ForegroundColor Red
    exit 1
}

Write-Host "Setting up local ffmpeg..." -ForegroundColor Yellow
Install-Ffmpeg

Write-Host ""
Write-Host "Running smoke test..." -ForegroundColor Yellow
& python -c @"
import torch
print(f'PyTorch: {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    cap = torch.cuda.get_device_capability(0)
    print(f'Compute capability: {cap}')
    if cap[0] >= 10:
        print('Blackwell or newer detected - GPU acceleration ready')
    else:
        print(f'Warning: compute capability {cap} - may still work but not optimal')
else:
    print('Warning: CUDA not available. Separation will use CPU (slow).')
"@

Write-Host ""
Write-Host "Checking TTS imports..." -ForegroundColor Yellow
& python -c @"
try:
    from omnivoice import OmniVoice as _ov
    print('  omnivoice (k2-fsa): OK')
except Exception as e:
    print(f'  omnivoice (k2-fsa): WARN ({e})')
try:
    from qwen_tts import Qwen3TTSModel as _q
    print('  qwen_tts.Qwen3TTSModel: OK')
except Exception as e:
    print(f'  qwen_tts: WARN ({e})')
try:
    import anidub
    print('  anidub (project): OK')
except Exception as e:
    print(f'  anidub: WARN ({e})')
"@

Write-Host ""
Write-Host "=== Installation complete! ===" -ForegroundColor Green
Write-Host ""
Write-Host "To run:" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "  anidub-test-voice" -ForegroundColor White
Write-Host "  (first run downloads ~2.45 GB OmniVoice model + ~3.4 GB Qwen3-TTS model)" -ForegroundColor DarkGray
Write-Host ""
