# Windows Release 安装包

这是 EchoLingo 的 Windows 安装脚本。Release ZIP 不携带 `.venv`、API Key 或 AI 模型；构建流程会把可直接运行的 Tesseract OCR 运行时（英文 `eng` 与方向检测 `osd` 数据）放入 `tools/tesseract/`。FFmpeg 仍由脚本通过 `winget` 安装。

在解压后的目录中，以 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\installer\install.ps1 -InstallAll
.\installer\start.ps1
```

参数说明：

- `-InstallOptional`：安装 Docling 与 EasyOCR。它们会带来较大的 PyTorch 依赖和首次模型下载。
- `-InstallNativeTools`：通过 winget 安装 FFmpeg（`Gyan.FFmpeg.Shared`）；如果是 Release 包，Tesseract 优先使用包内版本，不再重复安装，只有源码目录缺少包内运行时才回退到 `UB-Mannheim.TesseractOCR`。
- `-InstallPython`：缺少 Python 3.11 时，通过 winget 安装 `Python.Python.3.11`。
- `-InstallAll`：同时启用以上三个选项。
- `-ForceEnv`：用 `.env.example` 覆盖已有 `.env`。默认不会覆盖已有配置。
- `-IndexUrl https://.../simple`：为 pip 指定包源。

安装脚本只写入当前 Release 目录的 `.venv` 和 `.env`。Tesseract 的上游许可与打包说明见 `tools/tesseract/BUNDLE-NOTICE.txt`；FFmpeg、Docling、EasyOCR 的许可与模型说明见仓库根目录的 `THIRD_PARTY_LICENSES.md`（如该文件存在）。
