[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Server = Join-Path $RepoRoot "backend\fastapi_server.py"
$BundledTesseractDir = Join-Path $RepoRoot "tools\tesseract"
$BundledTesseract = Join-Path $BundledTesseractDir "tesseract.exe"
$BundledFfmpegDir = Join-Path $RepoRoot "tools\ffmpeg"
$BundledFfmpeg = Join-Path $BundledFfmpegDir "ffmpeg.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "尚未安装 EchoLingo。请先运行 .\installer\install.ps1。"
}
if (-not (Test-Path -LiteralPath $Server)) {
    throw "找不到后端入口: $Server"
}

Set-Location -LiteralPath $RepoRoot
if (Test-Path -LiteralPath $BundledTesseract) {
    $env:Path = "$BundledTesseractDir;$env:Path"
    $tessdata = Join-Path $BundledTesseractDir "tessdata"
    if (Test-Path -LiteralPath (Join-Path $tessdata "eng.traineddata")) {
        $env:TESSDATA_PREFIX = $tessdata
    }
    Write-Host "使用 Release 包内 Tesseract：$BundledTesseract" -ForegroundColor DarkGray
}
if (Test-Path -LiteralPath $BundledFfmpeg) {
    $env:Path = "$BundledFfmpegDir;$env:Path"
    Write-Host "使用 Release 包内 FFmpeg：$BundledFfmpeg" -ForegroundColor DarkGray
}
Write-Host "EchoLingo 正在启动：http://localhost:5173" -ForegroundColor Green
& $VenvPython $Server
