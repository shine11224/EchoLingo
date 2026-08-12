---
type: 自建
source: English Learning Tool production migration
---

# 东京单用户部署方案

## 目标与边界

在东京服务器将应用以 HTTPS 站点提供给单一使用者；入口以 Caddy Basic Auth 保护，应用端口不对公网暴露。课程、词库、SQLite 数据库、上传媒体和 AI 配置分别随 Docker 卷持久化，镜像或容器更新不会清除数据。

当前服务器为 2 vCPU / 3.6 GiB RAM / 25 GiB 可用磁盘，因此此部署支持学习、阅读导入、外部 AI 调用和 Edge TTS；不启用本地 Hy-MT llama-server、faster-whisper 大规模转写或 MFA 对齐任务。媒体文件须定期清理，建议可用空间低于 8 GiB 时停止新增视频课程。

## 执行顺序

1. 为一个专用子域设置 DNS A 记录指向东京服务器公网 IPv4，并等待解析。
2. 在服务器部署目录执行 `sh deploy/bootstrap-auth.sh <域名>`，它会生成仅当前用户可读的 `.env` 和一次性显示的访问密码。
3. 首次迁移本机词库、SQLite 数据和 API 配置时，先在本机仓库根目录执行 `sh deploy/package-data.sh <迁移目录>` 生成 `essential-data.tar.gz`（含完整 resources/ 与 .env），上传到服务器后执行 `sh deploy/seed-data.sh <迁移目录>`；随后执行 `docker compose up -d --build`。Caddy 仅在 DNS 已生效且 80/443 可达时自动签发 HTTPS 证书。
4. 验收 `/health`、浏览器认证、一次手机阅读课程打开和一次外部 AI 请求；随后备份 `echolingo_resources`、`echolingo_output`、`echolingo_config` 三个卷。

## 多用户与认证

`ELT_AUTH_ENABLED=1` 时启用应用内登录（cookie session，30 天）。用户数据按 `resources/users/<username>/vocab.db` 物理隔离；用户/会话/邀请码存共享 `resources/auth.db`。注册必须邀请码：管理员登录后 `POST /api/auth/invites` 生成，或服务器上 `docker exec -w /app app-app-1 python -c "import sys; sys.path.insert(0,'backend'); from webapp.auth import store; print(store.create_invite_code(created_by='may'))"`。Caddy 不再做 basic_auth，认证完全在应用层。

## 回滚与升级

升级前导出 `echolingo_resources`、`echolingo_output`、`echolingo_config` 三个卷；若新镜像异常，执行 `docker compose down` 后以保留的上一镜像重新 `up -d`，持久数据不会被删除。不要使用 `docker compose down -v`，它会删除学习数据和配置。

## YouTube 代理（mihomo）

东京机房 IP 被 YouTube 硬封（cookies/PO token/换 client 均无效，已实测），因此 compose 内含 mihomo 代理服务，应用通过 `YOUTUBE_PROXY=http://mihomo:7897` 仅将 YouTube 流量走代理，其余流量直连。

- 配置在仓库外 `/home/ubuntu/mihomo/config.yaml`（chmod 600），由订阅链接拉取：`curl -sk -A 'mihomo/v1.18' <订阅URL> -o /home/ubuntu/mihomo/config.yaml && docker restart app-mihomo-1`。
- 订阅节点失效或 YouTube 再次拦截时，先按上式刷新订阅重启 mihomo，再排查应用层。
- cookies 刷新仍走 `deploy/refresh_youtube_cookies.py`，与代理互不依赖。

32. 百度网盘导入：公开版由克隆者自行授权：运行 `bdpan login --accept-disclaimer --get-auth-url`，浏览器确认后运行 `bdpan login --accept-disclaimer --set-code <32位授权码>`，再用 `bdpan whoami --json` 验证；Docker 部署时把命令改为 `docker exec -it app-app-1 bdpan ...`。私有多人云端也可由管理员在“账号设置 → 百度网盘”完成同一授权引导。token 只保存在 bdpan 配置目录（Docker 为 `echolingo_bdpan_config` 卷），仓库与任务数据库均不保存凭据；分享提取码仅驻留内存。网盘账号为单部署实例共享账号，普通用户只能提交分享链接，单用户/管理员可从“我的网盘”完整目录中单选文件。单文件默认上限 1GB；持久化 FIFO 队列默认并发下载 2、每用户一个活跃任务、普通用户每日 3GB，并预留文件大小 + 2GB 磁盘空间。配置项：`ELT_BAIDU_PAN_MAX_MB=1024`、`ELT_BAIDU_PAN_DOWNLOAD_CONCURRENCY=2`、`ELT_BAIDU_PAN_DAILY_GB=3`、`ELT_BAIDU_PAN_ENABLED=0`。

## 积分配置（2026-08-08 起）

- `ELT_CREDIT_MODE=off|shadow|enforce`（默认 `shadow`）：`off` 完全不计费；`shadow` 只记录预计积分与真实 usage、余额不变；`enforce` 真实预留/扣减。非法值启动即报错（fail closed），不会静默变免费。
- `ELT_TRIAL_CREDITS` / `ELT_ADMIN_TRIAL_CREDITS`：注册 onboarding 一次性赠分（普通用户/管理员分别配置，非负整数，每用户仅到账一次）。
- 流水与余额存共享 `resources/auth.db`（credit_ledger + reservations）；用户余额 = ledger + active reservations 实时重算，无冗余余额列需要对账。
- 上线节奏建议：先 `shadow` 跑至少一周核对流水与真实 API 成本，再切 `enforce`。

## 管理员私有路由与浏览器上传

- 普通用户首页只见浏览器上传音视频与 Reading 上传；YouTube/B 站/服务器路径入口集中在 `GET /admin/import`（`require_admin` 门控，非 admin 一律 404，对应 API 同样 404，非仅前端隐藏）。
- 管理员判断以 `resources/auth.db` users.is_admin 为准；目前唯一 admin 是 may，新增 admin 只能直接改库，无自助提权入口。
- 每用户媒体物理隔离：上传落 `resources/users/<username>/uploads/`，产物落 `resources/users/<username>/output/`；`/output/` 静态路由按当前用户 scope 解析，拒绝 `..`、绝对路径与他人 upload_id。
- 旧云端全局 output 迁移：`python scripts/migrate_cloud_output_to_admin.py`（默认 dry-run，确认后加 `--apply --username may`），按 v2_lessons 归属证据复制，绝不删除旧目录。

## 课程问答 RAG

- 无需额外配置与外部向量库：检索只读当前课程 SQLite 字幕/Reading 句，回答带 coverage（full/partial/none）与证据卡跳转；无跨课程、跨用户数据路径，多用户隔离由现有 scope 机制保证。
