[CmdletBinding()]
param(
    [switch]$InstallOptional,
    [switch]$InstallNativeTools,
    [switch]$InstallPython,
    [switch]$InstallAll,
    [switch]$ForceEnv,
    [string]$IndexUrl = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$BundledTesseractDir = Join-Path $RepoRoot "tools\tesseract"
$BundledTesseract = Join-Path $BundledTesseractDir "tesseract.exe"
$BundledFfmpegDir = Join-Path $RepoRoot "tools\ffmpeg"
$BundledFfmpeg = Join-Path $BundledFfmpegDir "ffmpeg.exe"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Tool {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败 ($LASTEXITCODE): $File $($Arguments -join ' ')"
    }
}

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Enable-BundledTesseract {
    if (-not (Test-Path -LiteralPath $BundledTesseract)) {
        return $false
    }

    $env:Path = "$BundledTesseractDir;$env:Path"
    $tessdata = Join-Path $BundledTesseractDir "tessdata"
    if (Test-Path -LiteralPath (Join-Path $tessdata "eng.traineddata")) {
        $env:TESSDATA_PREFIX = $tessdata
    }
    return $true
}

function Enable-BundledFfmpeg {
    if (-not (Test-Path -LiteralPath $BundledFfmpeg)) {
        return $false
    }

    $env:Path = "$BundledFfmpegDir;$env:Path"
    return $true
}

function Find-Python311 {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $candidate = (& $launcher.Source "-3.11" "-c" "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
        if ($candidate -and (Test-Path -LiteralPath $candidate.Trim())) {
            return $candidate.Trim()
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $version = (& $python.Source "-c" "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null | Select-Object -First 1)
        if ($version -eq "3.11") {
            return $python.Source
        }
    }

    return $null
}

function Install-WingetPackage([string]$Id) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        throw "未找到 winget。请从 Microsoft Store 安装 App Installer，或手动安装包管理器中的 $Id。"
    }

    Invoke-Tool $winget.Source @(
        "install", "--id", $Id, "--exact", "--source", "winget",
        "--accept-package-agreements", "--accept-source-agreements",
        "--silent", "--disable-interactivity"
    )
    Refresh-Path
}

if ($InstallAll) {
    $InstallOptional = $true
    $InstallNativeTools = $true
    $InstallPython = $true
}

Set-Location -LiteralPath $RepoRoot
$hasBundledTesseract = Enable-BundledTesseract
if ($hasBundledTesseract) {
    Write-Host "Release 包内已包含 Tesseract，优先使用本地运行时。" -ForegroundColor Green
}
$hasBundledFfmpeg = Enable-BundledFfmpeg
if ($hasBundledFfmpeg) {
    Write-Host "Release 包内已包含 FFmpeg，优先使用本地运行时。" -ForegroundColor Green
}

Write-Step "检查 Python 3.11"
$Python = Find-Python311
if (-not $Python -and $InstallPython) {
    Write-Host "未找到 Python 3.11，尝试通过 winget 安装。" -ForegroundColor Yellow
    Install-WingetPackage "Python.Python.3.11"
    $Python = Find-Python311
}
if (-not $Python) {
    throw "需要 Python 3.11。请安装 Python 3.11，或重新运行并添加 -InstallPython。"
}
Write-Host "Python: $Python"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Step "创建虚拟环境"
    Invoke-Tool $Python @("-m", "venv", $VenvDir)
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "虚拟环境创建失败: $VenvPython"
}

Write-Step "安装核心 Python 依赖"
$pipArgs = @("-m", "pip", "install", "-r", (Join-Path $RepoRoot "requirements.txt"))
if ($IndexUrl) {
    $pipArgs += @("--index-url", $IndexUrl)
}
Invoke-Tool $VenvPython $pipArgs

if ($InstallOptional) {
    Write-Step "安装 Docling 与 EasyOCR 可选组件"
    $optionalArgs = @("-m", "pip", "install", "-r", (Join-Path $RepoRoot "requirements-optional.txt"))
    if ($IndexUrl) {
        $optionalArgs += @("--index-url", $IndexUrl)
    }
    Invoke-Tool $VenvPython $optionalArgs
}

if ((Test-Path -LiteralPath ".env.example") -and ((-not (Test-Path -LiteralPath ".env")) -or $ForceEnv)) {
    Write-Step "从公开模板创建 .env"
    Copy-Item -LiteralPath ".env.example" -Destination ".env" -Force
}

if ($InstallNativeTools) {
    Write-Step "安装 FFmpeg 与 Tesseract"
    if (-not $hasBundledFfmpeg -and -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Install-WingetPackage "Gyan.FFmpeg.Shared"
    }
    if (-not $hasBundledTesseract -and -not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
        Install-WingetPackage "UB-Mannheim.TesseractOCR"
    }
    if ($hasBundledFfmpeg) {
        Write-Host "跳过 winget FFmpeg 安装（使用 Release 包内版本）。"
    }
    if ($hasBundledTesseract) {
        Write-Host "跳过 winget Tesseract 安装（使用 Release 包内版本）。"
    }
}

Write-Step "环境检查"
# 纯 ASCII 且走 stdin 传代码：避免 PS 5.1 原生命令参数/管道编码破坏内嵌引号与中文
$envCheck = @'
import importlib.util, os, shutil, sys
from pathlib import Path
repo = Path(sys.prefix).parent
vcredist = repo / "tools" / "vcredist"
if vcredist.is_dir() and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(str(vcredist))
print("Docling: " + ("installed" if importlib.util.find_spec("docling") else "missing"))
print("EasyOCR: " + ("installed" if importlib.util.find_spec("easyocr") else "missing (optional)"))
print("Tesseract: " + (shutil.which("tesseract") or "NOT FOUND (scanned-PDF OCR unavailable)"))
print("FFmpeg: " + (shutil.which("ffmpeg") or "NOT FOUND (audio/video pipeline unavailable)"))
try:
    import ctranslate2
    print("CTranslate2: " + ctranslate2.__version__)
except OSError as e:
    print("CTranslate2: LOAD FAILED (Whisper unavailable): " + str(e)[:80])
try:
    from zoneinfo import ZoneInfo
    ZoneInfo("Asia/Shanghai")
    print("tzdata: ok")
except Exception:
    print("tzdata: MISSING (Baidu Pan import unavailable)")
'@
$envCheck | & $VenvPython -

Write-Host "`n安装完成。启动命令：" -ForegroundColor Green
Write-Host "  .\installer\start.ps1"
Write-Host "如需安装全部可选组件和原生工具：" -ForegroundColor Yellow
Write-Host "  .\installer\install.ps1 -InstallAll"
