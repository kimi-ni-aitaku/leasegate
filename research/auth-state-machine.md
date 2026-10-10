# 授权状态机与三种二维码场景

## A：本系统订单授权（完整实现）

`PAID → CDK_REDEEMED → Activation → QR CREATED → SCANNED → CONFIRMED → Lease ACTIVE → EXPIRED / REVOKED`

卡密高随机，仅保存 SHA-256；卡密兑换时消耗一次使用次数。浏览器取得 HttpOnly Activation 所有权会话。生成 QR Session 后，二维码内包含 ID、随机 secret、到期值和 HMAC 签名。凭据放在 URL fragment，避免进入 HTTP URL 和 Referer。

扫码请求仅标记 SCANNED。用户点击确认后，数据库事务锁定 Activation、QR、Order、Resource，建立唯一的订单 Lease。只有原 Activation 浏览器能领取服务 Session；扫描二维码的另一台设备只负责确认。CDK、二维码和 Activation 会话都是 bearer 凭据，持有者具备对应权限；不要分享。

SSE 通过原 Activation 会话鉴权。浏览器关闭摄像头、显示倒计时；服务端权限以数据库状态与时钟为准。

## B：第三方官方 Device Authorization（可配置实现）

`CREATE_DEVICE_CODE → WAIT_USER → authorization_pending / slow_down → AUTHORIZED → EXCHANGED → Lease`

服务器申请 device_code；浏览器只得到 user_code、官方 verification_uri、有效期和间隔。用户本人在官方认证页面确认。Worker 尊重 interval 与 slow_down，在服务器交换 Token 并加密。外部上传的 device_code 不直接作为授权证明。

此项目无默认第三方账号或 OAuth 客户端。使用配置项接入实际官方服务后才能使用 B。测试通过 MockTransport 核对协议行为，不能当作真实第三方联调成功。

## C：另一账号代确认外部客户端二维码（不支持）

`EXTERNAL_CLIENT_QR → UnsupportedProvider → 501`

不得由正常扫码登录流程推导出外部二维码确认、指定设备踢出或代扫能力。未知 URL 不执行网络请求、不跟随跳转；相似域名不会被识别为可信 Provider。

## Lease / Session

`ACTIVE → EXPIRED` 或 `ACTIVE → REVOKED` 为单向且幂等的状态迁移。事务内删除 refresh Session、记录撤销 JTI、减少资源计数、写审计。Access JWT 最长 900 秒，并绑定当前 Session 的 JTI。刷新旋转 refresh secret 与 JTI，旧 JWT 随即不可用。

有效期内的普通 bearer JWT 可以重复调用 API；“防重放”在这里指拒绝已轮换、已撤销、过期和伪造 JTI，不承诺让每个有效 Access Token 只能使用一次。

Redis ZSET 是可重建的索引，数据库是持久化任务来源。Worker 每轮查询到期 Lease，再重建 Redis 索引。Worker 或 Redis 不可用时，API 的 `require_active_lease` 仍即时拒绝到期权限；未被访问的到期 Lease 在 Worker 恢复后释放资源。

远程撤销使用独立加密重试队列。没有配置官方撤销端点时记录 `local_only` 并删除服务端凭据，不宣称注销第三方设备。
