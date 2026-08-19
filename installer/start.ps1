[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Server = Join-Path $RepoRoot "backend\fastapi_server.py"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "尚未安装 EchoLingo。请先运行 .\installer\install.ps1。"
}
if (-not (Test-Path -LiteralPath $Server)) {
    throw "找不到后端入口: $Server"
}

Set-Location -LiteralPath $RepoRoot
Write-Host "EchoLingo 正在启动：http://localhost:5173" -ForegroundColor Green
& $VenvPython $Server
