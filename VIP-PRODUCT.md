# VIP 扫码产品：交付内容与接入说明

本次在 LeaseGate 上新增买家入口 `/qr` 与运营后台 `/vip-admin`。账号池、卡密业务、二维码提交、持久化任务和运营界面已实现；**真实夸克自动扫码确认尚未接通，不能当作已经达到卖家的可用状态。** 默认未配置授权端时，买家页面明确显示服务暂未开放，不扣扫码次数。

当前接收已核对的夸克电脑**网页**登录二维码（`client_id=532`）。其他客户端的二维码格式、卖家的一键写入 PC 客户端、原生签名生成和指定设备注销没有实现。手机可以打开买家扫码页面；授权目标仍是电脑。

## 已实现的业务能力

| 能力 | 本次结果 |
| --- | --- |
| 批量账号导入 | 每批最多 50 条 Cookie 或自有授权端账号引用；AES-GCM 加密落库 |
| 账号可用性检查 | 未接授权端时直接检查官方网页登录态与会员类型，标记为 `web_only`，不会进入授权库存；授权端完成身份、会员、容量和目标身份核验后才进入库存 |
| 更新失效凭据 | 后台加密替换 Cookie 或授权端引用；重新检查；有占用或待核对会话时拒绝替换 |
| 防止重复库存 | 凭据去重；检查时根据真实账号主体去重，避免不同 Cookie 重复计容量 |
| 批量卡密 | 每批最多 100 张；默认 24 小时、7 次、1 台电脑；可配置未激活有效期 |
| 临时链接 | `/qr#card=VG-…`；服务端保存卡密哈希，浏览器读取后清除地址中的卡密 |
| 扫码输入 | 手机摄像头、截图上传、拖拽、粘贴；优先浏览器解码，失败后服务端解码 |
| 次数与计时 | 确认授权后扣一次；首次确认开始计时；重试同一任务不会重复计费 |
| 设备限制 | 依赖授权端提供经过平台验证的目标电脑身份；缺少该证据时拒绝授权 |
| 账号分配与换号 | 按空闲比例分配，事务预留；明确判定账号不可用后切换其他账号 |
| 补充验证 | 短信验证通过买家入口提交；会员确认、人机验证由官方授权设备完成 |
| 超时与重启 | 网络超时保留库存预留并查询原任务；不会盲目重复发起授权 |
| 到期处理 | 到期禁止继续使用卡密；外部设备注销单独重试，未确认注销时保留容量占用 |
| 管理与审计 | 账号、卡密、任务、设备绑定列表；停用；使用现有管理员鉴权、CSRF 与限流 |

卡密创建时不固定账号。首次授权时绑定账号和目标电脑；明确确认旧账号不能继续授权后，可在同一电脑上切换账号。首次成功后有效期不因重扫延长。

卡密原文只在生成响应中出现，后台历史列表不能再次取回；请使用“下载卡密 TXT”保存。导入的账号凭据不返回买家，不出现在后台列表或审计正文中。新入口独立于原有订单/CDK 演示流程，本版没有把商城付款 Webhook 自动映射到新 VIP 卡密。

## 卖家的公开前端可以确认什么

本次读取的是公开页面及其已加载的 JavaScript，没有提交卖家的二维码登录接口。

- 用户提供的卡密页面（不在交付源码中公开卡密标识）显示到期时间、剩余次数、公告、手机扫码和上传入口。
- [夸克前端模块](https://www.yy21.top/js/7053.07497f53.js)在扫码后把二维码内容与卡密、设备标识提交到 `/api/qrLogin`，根据结果更新界面。该流程没有展示把账号池 Cookie 发给买家的逻辑。
- 同一模块还包含一条本机中继与服务端兑换 App Token 的 PC 客户端路径。它与二维码代确认是不同的路径，本版未实现。
- [页面模块](https://www.yy21.top/js/1408.f55d7a46.js)负责卡密检查与入口选择。浏览器设备标识只能说明扫码页面所在浏览器，单凭它无法证明电脑端只有一台设备。

卖家的后端、原生签名来源、账号池组织方式和设备身份校验无法从这些脚本确定。不能把“卖家可以用”推导成“普通 Web Cookie 一定能完成所有接口”。

夸克[官方扫码确认脚本](https://image.quark.cn/s/uae/g/1y/broccoli/DuJrlezmS/EATJRLSvv/bundle.752708baa310214926fb.js)使用原生账号桥取得 `sign_wg`、`kps_wg`、`vcode`，再调用确认接口。现有仓库的 Cookie 单独确认失败记录仍然成立，但它也不能证明所有其他授权通道都不存在。

对于电脑网页二维码，买家保留原始官方登录窗口，官方窗口完成票据交换并建立 Cookie。买家入口展示确认结果即可，不必复制账号池的 Cookie 到买家浏览器。

## 启动与管理

使用现有启动方式：

```sh
docker compose up -d --build
```

首次启动或升级时，入口脚本运行 Alembic；本次新增固定迁移 `0002_vip_product.py`，不删除旧表。打开 `/vip-admin`，用现有管理员密钥登录：

```sh
docker compose exec api python scripts/show_admin.py
```

先导入账号，配置自有授权端，点击“检查”。账号状态变成“可用”后生成卡密，保存临时链接并交付给买家。不要将真实 Cookie 填入卖家的页面。

生产环境沿用原有 PostgreSQL、Redis、HTTPS 和持久化密钥配置。本次新增配置：

```dotenv
VIP_GATEWAY_URL=https://your-owned-gateway.example/quark
VIP_GATEWAY_SECRET=replace-with-at-least-32-random-characters
VIP_NOTICE=仅支持在电脑端登录。请保持电脑上的登录二维码窗口打开。
```

Compose 将这些配置同时提供给 API 和 Worker。URL 必须固定 HTTPS，不允许凭据、查询参数或 fragment；请求不跟随跳转。没有配置时仅可管理账号和卡密，扫码授权关闭。

## 自有授权端协议

`VIPGateway` 是对运营者自有授权服务的接入适配器，**以下是本产品定义的接口合约，不是夸克提供的开放 API，也没有附带签名服务。** 接入服务必须实际拥有有效会员会话、合法的确认能力、任务账本，以及目标电脑身份的验证来源。不能把二维码 token、浏览器指纹或手机号页面标识冒充稳定的电脑身份。

所有请求为 `POST {VIP_GATEWAY_URL}/{operation}`，JSON 正文，含 `Authorization: Bearer {VIP_GATEWAY_SECRET}` 和 `Idempotency-Key`。响应必须 HTTP 200、JSON 对象、最多 64 KB。网络错误、跳转或结构错误不会视为登录成功。账号凭据只发往配置的自有授权端。

### `check`

输入含 `account_id`、`credential_type`、`credential`。只有全部满足以下信息才可用：

```json
{
  "authenticated": true,
  "membership_valid": true,
  "account_subject": "stable-platform-account-subject",
  "device_limit": 1,
  "target_identity_supported": true
}
```

`account_subject` 必须来自已认证的平台账号，数据库仅保存其哈希。平台身份重复时第二条库存不会启用。实际容量取导入容量与授权端核实容量的较小值。

### `inspect`

输入含 `provider=quark` 和规范化的二维码 `raw`。它是无授权副作用的检查，返回：

```json
{
  "target_verified": true,
  "target_kind": "desktop",
  "target_id": "stable-verified-target-computer"
}
```

缺少可靠的 `target_id` 时，本系统返回 `target_device_unverifiable`，不会用随机标识补齐。

### `authorize`、`status`、`answer`

输入含 `task_id`、`provider`、`raw`、`target_hash`、账号凭据；`answer` 另含一次性验证内容。`target_hash` 是 `SHA256("quark-device:" + target_id)`。

每个任务与账号组合使用固定幂等键。`authorize` 必须将操作结果持久化，重复调用不能产生额外登录。`status` 只查询原操作，不能重新发起登录。`answer` 与原任务共用幂等键，operation 在服务端账本中另行区分；验证码提交中断后，本系统查询原任务，不重放验证码。

支持的响应：

```json
{
  "state": "authorized",
  "target_id": "stable-verified-target-computer",
  "session_ref": "private-reference-to-this-device-session",
  "login_verified": false,
  "revoke_supported": false
}
```

- `authorized` 必须表示平台确认已接受，且能对应到预检目标。`session_ref` 标识这个目标设备的会话，不能是全账号注销凭据。
- `login_verified=true` 只能在确实验证买家电脑已建立官方登录会话时返回。否则界面只显示“授权已确认，请回到电脑查看登录结果”。
- `pending`：查询原操作继续等待。
- `challenge`：附 `kind=sms|owner_confirmation|captcha`。短信内容短期加密保存，提交后从任务中删除；其他验证在官方授权端完成。
- `account_unavailable`：必须是明确、无新增授权副作用的账号不可用结果。系统才能释放未授权预留并切换账号。
- `rejected`、`expired`、`not_authorized`：必须是账本确认不存在未决授权的终态，系统才能释放预留。
- 未知状态、目标不一致和请求超时进入 `UNCERTAIN`；容量继续占用，直到核对清楚。

### `revoke`

输入含账号凭据和加密保存的 `session_ref`。幂等键为绑定 ID。只允许注销这个目标设备，返回 `{"revoked": true}` 后才释放占用。

不存在可靠注销能力时，`revoke_supported=false`。到期会停用本系统卡密，但外部会话状态记为 `remote_unconfirmed`，继续占用库存。不能宣称夸克已被自动踢下线。

## 本次验证

后端：91 项通过、3 项跳过（需要额外基础设施或实时测试开关）；包括迁移一致性、原有授权流程、21 项 VIP 测试和 8 项官方客户端交换测试。自动测试使用明确的测试授权端与 MockTransport，不连接真实夸克。

2026-10-11 的真实联调另见[新 Cookie 与客户端交换](research/quark-current-cookie-desktop.md)：取得官方电脑版 App Token，客户端接收并显示“确认登录”，最终登录未验证。管理员可在装有官方夸克客户端的本机运行 `python -m scripts.quark_desktop`，使用已加密导入的所有者 Cookie；支持 `--env-file`、`--cookie-file`。这是本机联调工具，尚未接入卡密自助流程；不会显示 Token 或自动点击协议。用户随后退出网页版，该 Cookie 再次检查已经失效。

前端：构建和 JavaScript 语法检查通过；本地浏览器验证后台登录、卡密生成、列表更新、临时链接读取与地址清理，并检查买家与后台的桌面布局。浏览器验收中修正了 24 小时输入的 step 校验和已打开页面处理新卡密 fragment 的问题。

本次未实测手机摄像头或移动设备；前端已编写响应式布局。

本机没有 Docker 或 PostgreSQL 命令，未执行容器构建或真实 PostgreSQL 并发测试。缺少真实会员授权端、稳定电脑身份来源和指定设备注销接口，因此没有执行真实夸克自动登录或到期退出验收。

达到卖家可用状态仍需：接入并验证真实授权端；用新生成的官方二维码完成登录；核实会员身份、同机重扫、跨电脑拒绝与到期行为；若需要 PC 客户端一键登录，再补齐官方客户端适配与本机中继。
