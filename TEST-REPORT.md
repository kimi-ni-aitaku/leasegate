# 实际执行的验收报告

日期：2026-10-10。代码、测试、迁移、前端构建与研究文件均已在本次任务中创建和执行，以下结果区分实际测试与尚未执行的部署检查。

## 运行环境

- macOS / ARM64，Python 3.13.3。
- 真实 PostgreSQL 16.2：在工作区创建临时实例，使用专用 `leasegate_test` 和 `leasegate_migration_test` 数据库。
- 真实 Redis 8.8.0：本任务专用实例及进程重启测试。Docker 配置的目标版本是 PostgreSQL 17 / Redis 7，未在本机执行该版本容器测试。
- Chrome + Playwright 1.58.2，前端使用 @zxing/browser 0.1.5，本地 esbuild 构建通过。

## 测试结果

| 验收内容 | 实际结果 |
| --- | --- |
| SQLite 快速后端测试 | 37 passed；2 个基础设施测试和 1 个真实等待测试默认跳过 |
| PostgreSQL 后端全集，含真实 Redis/DB 重启 | 39 passed；真实等待测试单独执行 |
| 真实 60 秒到期（实际等待） | 1 passed，约 61 秒；30 秒、59 秒正常，60 秒与 61 秒拒绝 |
| Chrome 浏览器流程 | 6 passed：订单/卡密、刷新待授权页面、跨浏览器扫码确认、SSE、API、管理撤销；上传/拖拽/粘贴均本地解码；移动布局；模拟摄像头 |
| 真实后台 Worker 进程 | 无买家到期请求时，直接读数据库确认 Lease 已 expired、Session 已删除；随后 API 返回 401 |
| 真实 API 进程停止/重新启动 | 现有 Cookie 会话继续有效，refresh 成功，管理员撤销后 API 拒绝 |
| 迁移往返 | SQLite 与 PostgreSQL 均验证 upgrade → downgrade → upgrade，数据库 schema 与模型无差异 |
| Python 后端覆盖率 | 最终 PostgreSQL 回归为 86%，覆盖 app 代码；不是安全认证或前端覆盖率指标 |
| 前端编译 | npm build 成功；依赖随 package-lock.json 固定，扫码无需加载 CDN |
| Compose 配置 | YAML、共享环境、依赖关系静态检查通过 |

## 已覆盖的核心断言

- 一次性 CDK 高随机、仅保存 Hash；12 个并发兑换仅一个成功。
- 12 个并发确认仅一个 Lease，资源不超容量；重复确认不会重复占用。
- 100 次签名 Webhook 重复事件仅一个订单/卡密/投递任务；重放 nonce 拒绝；订单内容冲突有明确错误。
- QR 过期、旧 QR 被重新生成取消、卡密过期、伪造签名、重复参数、来源/域名伪装、非支持 Provider 均被限制。
- 24 小时 Lease 的 Access Token 最长 900 秒；刷新旋转 secret/JTI，旧 refresh 与 JWT 不可使用。
- Worker 重复执行不重复释放资源；Redis 索引丢失可重建；Redis 停机仍由数据库到期；DB 停机 API 不放行。
- Admin 认证、CSRF、CORS、HttpOnly/SameSite、scope、JTI、Rate Limit 和生产配置检查。
- PNG/JPEG/WEBP 正常解码；伪 MIME、伪文件、超大图片拒绝。修复了 OpenCV 对部分 WEBP QR 不稳定的问题，使用 ZXing-C++ 兜底。
- 官方 Device Grant 的 pending、slow_down、兑换、加密保存、令牌不回传、撤销重试。本系统失效后第三方撤销失败也不恢复权限。
- 投递加密、失败重试、成功删除正文；SMTP 使用 STARTTLS。上述外部服务协议通过 Mock 验证，未向真实收件人发送。

## 开发阶段交付状态

| 阶段 | 交付 |
| --- | --- |
| Phase 1 | FastAPI、PostgreSQL、Redis、Docker Compose、健康检查、持久化卷 |
| Phase 2 | Order、CDK、Activation、Lease、Session、加密投递 Outbox |
| Phase 3 | Local QR、摄像头/图片/拖拽/粘贴、严格解析、确认页、SSE |
| Phase 4 | JWT、scope/JTI/Lease 验证、刷新旋转、服务端到期拒绝 |
| Phase 5 | ZSET、DB 对账、幂等 Worker、会话清理与资源释放 |
| Phase 6 | 订单、卡密、Lease、资源、任务、扫码、投递、审计后台 |
| Phase 7 | Local/OAuth/Unsupported/QuarkReference Adapter、官方协议可配置实现 |
| Phase 8 | 13 个参考源码文件固定提交与哈希，三份能力/状态机研究记录 |
| Phase 9 | 并发、安全、真实到期、基础设施重启、浏览器验收 |

## 明确的未执行项

本机没有 Docker CLI/Engine，因此未执行 Docker 镜像构建或 `docker compose up -d`。已提供 [compose_smoke.py](scripts/compose_smoke.py) 和 GitHub Actions，供具备 Docker 的环境实际检查容器启动、容器重启、60 秒到期及资源释放；不能把静态检查当作容器运行成功。

摄像头测试使用 Chrome 模拟视频输入；实物摄像头、手机跨设备 HTTPS 与网络可达性需在部署环境验收。官方 OAuth 测试使用 MockTransport；没有真实官方客户端、商城或 SMTP 凭据，因此未宣称完成实际第三方联调。Quark 外部二维码代确认保持明确的 501，不编造接口。

测试期间存在一条 Starlette TestClient 的 httpx 弃用提示；测试全部通过。Node/npm 主机版本提示没有影响前端构建或浏览器运行，项目 Docker 和 CI 固定为 Node 22。
## 后续：用户授权的夸克 Cookie 只读连接

新增 4 个测试通过，SQLite 全量为 **41 passed / 3 skipped**。覆盖格式歧义拒绝、部分解析标记、重复条目冲突、请求头注入拒绝、加密文件权限、管理员与 CSRF 边界、凭据删除、固定目标域名、跳转拒绝和响应脱敏。

真实只读联调：用户粘贴内容含 Markdown/HTML 转义，只恢复名称和值无歧义的 11 个 Cookie 条目，跳过 11 个歧义条目，私有运行目录加密保存。账号接口未验证到登录态，会员接口 HTTP 401。因此尚未完成有效会员账号连接，不能把此结果解释为原始 Cookie 一定过期；需用完整原始请求头替换后复测。

Chrome 验证后台连接面板与部分导入提示；使用虚构 token 的夸克格式二维码图片验证上传和浏览器识别。未提交真实官网登录确认；真实官网截图尚未收到。前端构建成功。此前 PostgreSQL、基础设施、60 秒到期测试结果为前一版结果，本次没有重新执行 Docker 或 PostgreSQL 联调。


## 真实官网二维码模拟买家测试（2026-10-10）

独立 Chrome 买家会话、演示订单、实际官网二维码截图：识别成功；Provider HTTP 501；本系统 pending/WAIT_USER，服务 HTTP 401；官网业务轮询状态 50004001，未返回 Service Ticket，仍显示扫码登录。实际未登录成功。Cookie 再查仍未验证、会员接口 HTTP 401。此前“真实官网截图尚未收到”的范围现已由自动访问官网并截图测试补充；没有成功完成第三方登录确认。复现脚本：`scripts/quark_buyer_smoke.mjs`，失败退出码 2。

## 官方确认适配器与纯网页实测更新

SQLite 全量 **49 passed / 3 skipped**。新增确认协议 MockTransport、只允许固定官方目标、签名缺失/过期拒绝、票据不回传、管理员权限、不会通过诊断接口创建 Lease、当前 account/info success/data/code 响应格式的测试。前端构建成功；最后的响应类型与时钟偏差保护变化通过 12 项相关测试。

真实网页确认测试：官方二维码生成成功；普通浏览器打开确认页最终显示下载 App。使用现有完整 Cookie、真实 token 且不编造 App 签名调用已核对的确认接口，业务状态 50001；随后轮询 50004001、无 Service Ticket。买家仍未登录成功。官方确认请求已实现于管理员联调入口，尚缺真实 App 签名来源；没有宣称 Mock 成功等于真实授权成功。

## 用户手机确认后的真实成功记录

用户在手机 App 扫描官网二维码并确认。MCP 控制 Chrome DevTools Network 导出的实际抓包显示：轮询业务状态 2000000，票据位于 data.members.service_ticket；account/info 使用 st/lw 兑换后 success=true、code=OK；再次账号查询成功；drive-member-h.quark.cn 的会员接口业务状态 200、member_type=SUPER_VIP；文件列表业务状态 200。官网进入 /list#/list/all，证明本次浏览器登录成功。

本次成功依赖用户手机确认；此前 Cookie 自动确认失败是另一条测试路径。手机签名确认请求没有被电脑浏览器捕获，未实现自动代确认或远程指定设备注销。根据实际响应修正会员检查主机和 mobilekps 账号响应兼容，13 项相关测试通过。原始 HAR 私有加密存储，交付记录只保留脱敏协议结构。

## 用户退出后：无手机确认的新买家接口实测

使用独立 HTTP Cookie 容器、新官方二维码、24 小时演示订单、真实 token 生成的二维码 PNG。演示订单兑换与图片识别成功，Provider 返回 409（缺少 App 签名）；官方 Cookie 确认业务码 50001；三次轮询均 50004001、无票据；本系统仍 pending/WAIT_USER、服务 HTTP 401。买家会员 HTTP 401/业务码 31001，买家未登录。

实测发现匿名 account/info 可以返回 success=true、code=OK，但 data 为空。因此成功判定必须包含非空账号身份数据、真正票据兑换及会员认证。补充此匿名响应回归检查；服务器已有 Cookie 的账号检查为 AUTH_ERROR:50051、会员 HTTP 401。此测试为接口层模拟，并非本轮浏览器截图自动化测试。

## 买家自己的二维码上传与可选会员端确认（最新）

新增订单绑定、跨订单二维码重放拒绝、同秒更换二维码、凭据加密清理、管理员图像访问控制、严格手机确认声明、确认幂等、买家反馈与第三方登录验证分离、本站到期释放、多标签页 CSRF 稳定性、默认自动模式不转人工待办测试。SQLite 全量 **61 passed / 3 skipped**；新增测试使用虚构二维码，不证明官方认证成功。

MCP 实际 UI：24 小时演示订单兑换后自动进入上传，官网已退出登录的 Chrome 新二维码截图上传成功，后台显示真实订单待确认；浏览器无错误日志。修复打开后台使买家 CSRF 失效的问题。随后用户改为仅 Cookie 自动确认：真实上传二维码的官方确认业务码 50001，原始官网仍显示扫码登录，账号 Cookie 检查 AUTH_ERROR:50051、会员 HTTP 401。已取消人工待办、未颁发 Lease，默认人工模式关闭。此轮没有真实手机确认，也没有真实登录成功。

此前 PostgreSQL、Docker、模拟摄像头浏览器测试是历史版本结果；本轮未重新执行。

## GitHub 上传前复验与客户端授权探索（2026-10-10）

当前 SQLite 全量重新执行：**61 passed / 3 skipped**，前端 esbuild 构建成功。三个跳过项为基础设施/真实等待测试；没有把历史 PostgreSQL、Docker 或浏览器测试计作本轮验收。

进一步按官方网页中真实存在的桌面授权流程，读取本机 `/desktop_info` 与 `/desktop_devicetoken`，用此前用户授权成功登录抓包中的真实 `mobilekps` 请求 `acquireAppToken`。HTTP 200，业务码 **50051**，未返回 App Token；本机官方客户端仍未登录，没有执行 `/desktop_postlogin`。这条路径也未建立新的授权会话。详见 [脱敏实测记录](research/quark-desktop-exchange.md)。

上传包按文件清单生成；检查运行密钥、Cookie 值、抓包中的二维码 token、Service Ticket 和 mobilekps 均未进入交付文件。原始 HAR、加密凭据、运行数据库、密钥和依赖目录不上传。自动确认和第三方设备到期注销仍未实现；默认自动模式明确拒绝缺失原生签名，不自动退回人工模式。

GitHub 首次 CI 的 PostgreSQL 17 后端测试为 **61 passed / 3 skipped**，前端构建成功。Docker 容器构建与 API 启动后，冒烟测试直接执行 `scripts/show_admin.py` 时找不到 `app` 模块；本机去掉 PYTHONPATH 后复现相同问题。已修复脚本的项目导入路径，并新增不依赖 PYTHONPATH、仅使用虚构密钥的独立进程回归测试。修复后本机全量 **62 passed / 3 skipped**。首次 Docker 冒烟流程未通过，修复版本以随后的 CI 实际结果为准。

## GitHub 集成验收成功（最新）

[第二轮 Actions 实际运行](https://github.com/kimi-ni-aitaku/leasegate/actions/runs/38017709671) 在提交 `00498f896fb2ded620680302a4d760845c459d11` 上 **Success**，总耗时 3 分 52 秒。不是仅上传了工作流：以下步骤在 GitHub Ubuntu Runner 实际执行并通过。

- PostgreSQL 17 后端全集：**62 passed / 3 skipped**。
- Node 22 安装锁定依赖并构建前端：成功。
- Docker 镜像构建、Compose API/Worker 启动、Redis/PostgreSQL/API/Worker 逐一重启、已有会话恢复、管理员撤销、真实 30/59/60/61 秒到期与资源释放：冒烟测试成功，耗时 1 分 58 秒。
- Chromium 浏览器：**5 passed / 1 skipped**。覆盖本地订单授权、刷新与 SSE、上传/拖拽/粘贴解码、移动布局及摄像头拒绝后的上传入口；模拟摄像头缺少 Y4M 夹具而跳过。浏览器管理员撤销分支未配置 ADMIN_TOKEN，但 Compose API 冒烟测试验证了管理员撤销。

这次云端结果补充了前文“本机未执行 Docker”的限制，并没有将本地演示扫码等同于夸克官网自动登录。夸克自动确认与指定设备到期注销仍未实现。
