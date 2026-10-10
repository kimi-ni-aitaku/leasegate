# LeaseGate

订单驱动的临时授权系统。FastAPI + PostgreSQL + Redis + ZXing 浏览器扫码。支持一次性卡密、扫码确认、SSE、15 分钟 Access JWT、默认 24 小时 Lease、自动到期、资源释放、审计与管理后台。

**当前夸克联调状态：买家上传官网二维码已实现，Cookie 自动确认尚未成功。** 实际 Cookie 确认返回业务码 `50001`；账号检查 `AUTH_ERROR:50051`；官方客户端授权交换返回 `50051`，未颁发 App Token。仍缺有效会员凭据及官方原生签名来源。本站 24 小时到期已实现，但没有夸克指定设备远程注销能力。通过 OpenCLI 咨询 ChatGPT、Gemini 并复核源码后，仍未发现经过验证的仅 Web Cookie 确认路径；按用户要求不接入手机 App。详见 [仅网页版路线复核](research/quark-opencli-review.md) 和 [客户端交换实测](research/quark-desktop-exchange.md)。

## 一键启动

需要 Docker Desktop / Docker Engine 和 Compose v2。在本目录执行：

```sh
docker compose up -d --build
docker compose ps
```

打开 **http://localhost**。首次启动自动生成四个密码学密钥并保存在 `runtime` 卷中；Alembic 自动执行初始迁移。服务端配置和数据库使用持久化卷，重启后保留。

演示流程：

1. 点击“创建测试订单”，选 60 秒商品，演示收件箱显示一次性卡密。
2. 点击“使用此卡密”，再点击“验证订单并创建授权”。
3. 使用另一设备扫码，或在同一浏览器点“打开确认页面”。也可把显示的二维码截图上传、拖入或粘贴到原页面。
4. 点击“确认激活此订单”。原页面通过 SSE 显示激活结果和倒计时。
5. 点击“验证服务 API”。60 秒后 API 返回 401，后台显示 expired，资源占用减少。

浏览器摄像头和直接读取剪贴板需要 HTTPS 或 localhost。手机扫电脑上的 localhost 地址不会指向电脑：跨设备测试时设置 `.env` 中 `PUBLIC_ORIGIN`、`CORS_ORIGINS` 为电脑的 HTTPS 域名，按需设置 `BIND_ADDRESS`，并用 HTTPS 反向代理。源码提供上传、粘贴和同浏览器确认方式方便本地验收。

管理后台：http://localhost/admin/orders。读取自动生成的管理员密钥：

```sh
docker compose exec api python scripts/show_admin.py
```

管理页面包括 orders、cdks、leases、resources、authorization-tasks、qr-sessions、deliveries、audit。登录后可提前撤销 Lease、添加资源；所有列表每页最多 100 条。CDK 原文和 Provider 凭据不在管理接口返回。

## 项目目录

```text
app/
  main.py          HTTP API、安全保护、SSE、图片解析
  models.py        Order → Activation → Task → Lease → Session
  service.py       事务、幂等、资源、权限、到期
  worker.py        到期、OAuth、投递与撤销重试
  security.py      AES-GCM、HMAC、JWT、高随机凭据
  qr.py            严格格式/来源识别，识别不等于授权
  providers/      local / oauth / quark_reference
  frontend.js      摄像头、上传、拖拽、粘贴、管理 UI
  static/          已构建的 JS、HTML、CSS
migrations/        固定的 Alembic schema migration
research/          源码核对记录和固定提交哈希
tests/             后端、并发、重启、真实 60 秒、浏览器测试
```

## 本地开发（无 Docker）

Python 3.13、Node 22；启动本机 Redis。SQLite 仅用于本地演示及快速测试，生产环境使用 PostgreSQL。

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/init_env.py
npm ci
npm run build
alembic upgrade head
PUBLIC_ORIGIN=http://127.0.0.1:8080 CORS_ORIGINS=http://127.0.0.1:8080 uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8080 --no-access-log
```

第二个终端用相同 `.env` 与 DATABASE_URL / REDIS_URL 运行 `python -m app.worker`。本地默认 SQLite 文件是 `leasegate.db`，Redis 默认 localhost:6379。数据库 schema 不由应用静默创建，需先执行迁移。

## 付款事件与自动投递

`POST /api/webhooks/order-paid` 接收：

```json
{"external_order_no":"platform-order-001","buyer_id":"buyer@example.com","product_id":"service-24h","amount":"9.90","timestamp":1790000000,"nonce":"at-least-16-random-characters"}
```

`X-Signature = HMAC-SHA256(WEBHOOK_SECRET, timestamp + "." + nonce + "." + 原始JSON请求体)`，十六进制小写。时间戳为 Unix 秒，允许服务器时间前后 300 秒；nonce 必须每次重新生成。原始请求体必须保持签名时的字节顺序与空格。外部订单号全局唯一；同订单相同内容幂等返回，内容冲突返回 409；相同 nonce 重放返回 409。商品与金额由服务端映射校验，不能从 Webhook 任意指定有效期。

付款事务同时写入 CDK Hash 和 AES-GCM 加密投递 Outbox。Webhook 不返回卡密原文。Worker 自动投递并重试，成功后删除加密正文。开发阶段使用演示收件箱；生产必须配置以下一个通道：

- `DELIVERY_URL` 和 `DELIVERY_SECRET`：固定 HTTPS 商城回调，由商城根据 buyer_id 发站内信。正文含 delivery_id、order_id、buyer_id、code、activation_url。请求头 `Idempotency-Key` 是 delivery_id；`X-Delivery-Signature` 是 `HMAC-SHA256(DELIVERY_SECRET, X-Delivery-Timestamp + "." + 原始正文)`。接收方应验签、防重放、按 delivery_id 去重。
- SMTP：配置 `SMTP_HOST/PORT/USER/PASSWORD/FROM`，连接强制 STARTTLS；此模式 buyer_id 必须为收件人邮箱。

投递是至少一次：外部发送成功、数据库提交前崩溃时可能重发。回调需去重；SMTP Message-ID 固定，但不保证邮箱服务商去重。卡密过期后停止发送并删除正文。没有接入实际商城或邮箱凭据，因此未向真实收件人发送消息。

## 官方 OAuth Device Provider

配置 `.env.example` 中 OAuth 项，仅用于官方明确支持 RFC 8628、运营者有权使用的客户端。Issuer、设备、Token 和可选撤销端点要求相同 HTTPS origin，禁止重定向。公开客户端发送 client_id；机密客户端采用 HTTP Basic 客户端认证。

浏览器在卡密兑换后可选“使用官方 OAuth Device 授权”，会取消本订单未确认的本地 QR。Worker 在服务端按 interval 轮询并处理 authorization_pending、slow_down、拒绝和到期。device_code、AccessToken、RefreshToken 不返回浏览器，不进入日志、HTML 或 localStorage。授权 Token 仅存于加密 Task/Lease；成功建立 Lease 后删除 Task 凭据。

当前 `/api/service` 是本系统受保护的示例服务，验证 Lease 后返回授权状态。接入具体业务服务时，应在该权限检查后调用对应业务 Provider；本项目没有默默接入夸克文件业务。OAuth 没有配置时返回 501，协议测试使用 MockTransport；真实平台联调须有官方客户端。

QuarkReference / UnsupportedProvider 不执行代扫、自动确认另一客户端、指定设备踢出。研究记录明确区分三种 QR 场景。

## 安全与故障行为

- CDK、QR secret、refresh secret 各使用 256 bit 随机值；数据库保存 Hash。投递与第三方凭据使用 AES-GCM，并以记录用途/ID 作 AAD。
- JWT 使用固定 HS256、issuer/audience、必须存在的 claims。每个请求核对 Session JTI、撤销表、Lease 状态、到期、买家、订单、scope。最长 900 秒且不超过 Lease 截止时间。refresh cookie 每次刷新旋转，旧 refresh 与 JWT 失效。
- 所有浏览器 POST 都要求签名 double-submit CSRF token；Webhook 独立签名。Cookie 使用 HttpOnly、SameSite=Strict；生产要求 Secure。CORS 为明确白名单；CSP 只允许本地静态脚本。
- Rate Limit 使用 Redis 原子计数。生产环境 Redis 限流不可用时，敏感 POST 返回 503；已激活的读请求仍依赖数据库验证。开发环境使用进程内限流兜底。
- 图片限制 5 MB、1600 万像素；校验 MIME 与实际 PNG/JPEG/WEBP 格式，关闭上传临时文件。本地图片解码成功时不上传图片；服务端使用 OpenCV，并以 ZXing-C++ 兜底。
- 到期 Worker 使用 ZSET 与持久化 DB 对账；Redis 丢失或重启会重建索引。DB 不可用时 API 返回 503，不继续放行权限。Worker 停止时，触发 API 的到期 Lease 仍立即撤销并释放资源；无人访问的记录由恢复后的 Worker 清理。
- 资源以最低使用比例分配，PostgreSQL 行锁与数据库容量约束避免超卖。一个订单最多一个 Lease。重复清理不会重复释放资源。
- 长期 Token 删除与本系统失效为强保证；第三方远程撤销是可观测的重试任务。没有官方撤销端点时记录 local_only，不宣称远程注销。
- 有效期内 Access bearer Token 可重复调用；不宣称防止有效 Token 持有者重用。撤销、轮换、伪造、过期 JWT 均被拒绝。
- 访问日志默认关闭，错误与审计不输出秘密；反向代理也应避免记录敏感请求头/正文。二维码和激活链接 fragment 不到达服务端 URL。

## 生产配置

复制 `.env.example` 到 `.env`，设置 `ENVIRONMENT=production`、`DEMO_ENABLED=false`、HTTPS 的 PUBLIC_ORIGIN/CORS_ORIGINS、`COOKIE_SECURE=true`、强数据库密码与投递渠道。应用启动时会拒绝不合格的生产配置。建议让 API 仍只监听 loopback，由 HTTPS 代理访问。

`runtime` 卷中的密钥由初始化服务生成一次，API/Worker 共享。备份数据库时同时安全备份此卷；丢失 AES 密钥会导致加密字段不可恢复。密钥轮换需要迁移已有密文并使旧会话失效；此版本不提供在线多密钥轮换。不要执行 `docker compose down -v` 清理正式数据。

## 测试与验收

```sh
pytest -q
RUN_REALTIME=1 pytest -q tests/test_realtime.py
```

快速测试使用可控服务端时钟，精确验证 30/59/60/61 秒；realtime 标记实际等待一分钟。

真实 PostgreSQL 测试需要**专用测试数据库**，库名必须以 `_test` 结尾。测试会清空这个库的应用表：

```sh
TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost/leasegate_test pytest -q
RUN_INFRA=1 pytest -q tests/test_infrastructure.py -k redis
```

临时 PostgreSQL 进程重启测试额外要求 `PG_TEST_CTL` 指向该临时实例的 pg_ctl、`PG_TEST_DATA` 指向其专用数据目录，并设置 TEST_DATABASE_URL、RUN_INFRA=1。不要指向已有业务数据库。

浏览器测试：

```sh
npx playwright install chromium
BASE_URL=http://127.0.0.1:8080 npm run test:e2e
```

可用 `CHROME_EXECUTABLE` 指定本机 Chrome；提供 ADMIN_TOKEN 时测试管理撤销。`CAMERA_QR_FILE` 为带有此系统有效 QR 的 Y4M 视频，用于 Chrome 模拟摄像头输入。实物手机、摄像头权限与 HTTPS 网络环境仍须部署后验收。

完整扫码、上传、拖拽、粘贴、模拟摄像头用例可自动生成实时 QR 视频夹具并执行：`python scripts/browser_qa.py --base-url http://127.0.0.1:8080`。需启用演示商品，PUBLIC_ORIGIN 与测试地址一致。

Docker 已安装时，运行 `python scripts/compose_smoke.py`：检查容器重启、完整 60 秒到期和资源释放。GitHub Actions 也配置了 PostgreSQL 测试及 Compose smoke。

本次具体执行结果与环境限制见 [TEST-REPORT.md](TEST-REPORT.md)。
## 夸克 Cookie 连接测试

管理后台 → 资源 → 夸克账号连接，支持加密保存、检查账号与会员、删除凭据。输入浏览器 Network 面板请求中的原始 Cookie 请求头，不能带 Markdown 星号；输入框保存后清空。Cookie 不会回传给买家，数据库列表和审计日志也不包含 Cookie。

只读检查仅访问固定的 `https://pan.quark.cn/account/info` 和 `https://drive-member-h.quark.cn/1/clouddrive/member`，不跟随跳转，不访问二维码中的地址。这里是用户明确授权的网页版账号检查，接口依据参考项目，不能视为夸克官方开放 API 合约。会员接口的类型码原样返回，不根据未知类型码宣称是付费会员。

Docker 中加密文件保存在单独的 cookie-data 卷，`QUARK_COOKIE_FILE=/var/lib/leasegate-cookie/quark-cookie.enc`；本地默认 `.private/quark-cookie.enc`。加密密钥丢失后需要重新导入。运行目录、Cookie 文件和密钥均不包含在交付压缩包中。

也可以在已配置 `.env` 的运行环境使用 `python -m scripts.quark_cookie import`，终端隐藏输入；`check` 检查、`status` 查看配置、`clear` 删除。Docker 中可使用 `docker compose exec api python -m scripts.quark_cookie import`。

上传或粘贴夸克官网登录二维码截图时，可以识别已研究的 URL 格式。默认自动模式缺少 App 签名时明确返回 HTTP 409，不创建人工待办或 Lease。账号 Cookie 可用不代表能够确认外部登录二维码；现在可使用 App 实际签名调用已核对的官方确认接口，但未得到签名时不能完成此操作，也没有第三方设备远程撤销保证。


真实官网买家流程可运行 `BASE_URL=http://localhost:8000 node scripts/quark_buyer_smoke.mjs`（需要 npm 开发依赖和 Playwright 浏览器；可指定 CHROME_EXECUTABLE）。脚本创建演示订单并上传实际官网二维码截图，检查 Provider、Lease 和官网登录态，未登录成功返回退出码 2；运行产物写入忽略的 test-results 目录。当前买家自动确认缺少 App 签名时返回 409，不宣称外部官网登录已实现。

### 官方确认请求联调

后台资源页的“官方确认请求联调”接受会员 App 实际确认请求中的原始二维码 URL、sign_wg、kps_wg、vcode；提交后输入清空。它调用已核对的官方 POST 表单接口，再查询二维码状态；不会生成签名、接受 Cookie 替代签名、把确认接受宣称为最终网页登录，也不会通过此诊断入口颁发 Lease。真实签名尚未提供，当前未完成实际确认联调。协议来源和具体阻塞见 [quark-official-confirmation.md](research/quark-official-confirmation.md)。

## 买家自己的夸克二维码与会员端确认

24 小时订单兑换后进入买家上传入口。二维码必须来自买家自己的夸克官网窗口，不能用历史登录票据或把会员 Cookie 注入买家浏览器冒充扫码成功。

默认 `QUARK_MANUAL_CONFIRMATION_ENABLED=false`：自动确认仍需要有效会员凭据和官方 App 签名来源。仅有网页 Cookie 的实际请求返回业务码 50001，不能宣称自动登录已实现。用户最后要求自动确认，运行环境保持此默认值。

若用户明确选择会员端手工确认，可将此配置设为 true 并重启服务。买家截图经过浏览器或服务器识别后，绑定已付款订单；同一 token 不能绑定其他订单，更换二维码会取消并清除旧待办。后台“夸克待确认”展示买家二维码；管理员在自己的会员 App 扫码并确认后，勾选完成并记录确认。二维码在服务端加密，图像仅管理员可取，过期/取消/确认后清除原文。此模式后台不轮询官方登录票据，票据由买家的原始官网窗口领取。

`OWNER_CONFIRMED` 只表示管理员记录了会员端确认；`BUYER_REPORTED_LOGIN` 只表示买家自己反馈已登录。两者 `buyer_login_verified` 均为 false，不能当作服务器验证了第三方登录。本站 Lease 从管理员记录确认时开始计时，24 小时后 API 拒绝请求并释放本站资源；没有官方设备撤销接口，不能保证夸克浏览器同步退出。

新增接口：买家 `POST /api/activation/quark/submit`、`/{task_id}/cancel`、`/{task_id}/login-reported`；管理员 `GET /api/admin/quark-requests`、`GET /api/admin/quark-requests/{task_id}/image`、`POST /api/admin/quark-requests/{task_id}/confirmed`（需要严格布尔 `phone_confirmed=true`）。买家状态从已有 `/api/activation/result` 获取。CSRF 令牌跨标签页复用有效签名值，失效时前端只重试一次；没有削弱 Origin 与签名检查。
