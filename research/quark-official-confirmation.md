# 手机扫码登录：真实抓包结果

2026-10-10，用户在手机 App 扫描电脑 Chrome 的夸克官网二维码并确认。通过 MCP 操作 Chrome DevTools Network，保留跳转前后的请求，导出并分析实际 HAR。

**浏览器登录成功，官方会员类型为 `SUPER_VIP`。** 浏览器进入 `https://pan.quark.cn/list#/list/all`，文件列表接口也返回业务状态 200。验证的是本次手机确认后建立的浏览器会话；此前粘贴 Cookie 的自动确认失败记录仍是独立测试结果。

| 步骤 | 实际接口 | 结果 / 参数结构 |
| --- | --- | --- |
| 生成二维码 | `GET https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin` | 业务状态 `2000000`；查询字段 `client_id`、`v`、`request_id` |
| 等待确认 | `GET https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken` | 等待时 `50004001`；旧二维码出现 `50004002`；新二维码经手机确认后 `2000000` |
| 取得票据 | 同上 | 成功票据位于 `data.members.service_ticket`；票据值已隐藏 |
| 兑换浏览器会话 | `GET https://pan.quark.cn/account/info` | 查询字段 `st`、`lw`；HTTP 200，`success=true`、`code=OK` |
| 再查账号 | `GET https://pan.quark.cn/account/info` | 查询字段 `fr`、`platform`；再次 `success=true`；数据字段 `nickname`、`avatarUri`、`mobilekps`、`config` |
| 查询会员 | `GET https://drive-member-h.quark.cn/1/clouddrive/member` | HTTP 200，业务状态 200，`code=0`，`member_type=SUPER_VIP` |
| 获取文件列表 | `GET https://drive-pc.quark.cn/1/clouddrive/file/sort` | HTTP 200，业务状态 200，`code=0`；不导出文件名称或内容 |

会员请求的查询字段为 `pr`、`fr`、`fetch_subscribe`、`_ch`、`fetch_identity`。已经按本次抓包修正系统的会员检查主机和账号响应兼容，相关 13 项测试通过。

电脑浏览器只能捕获自身网络请求，未捕获手机端 `loginWithKpsAndQrcodeToken` 请求或原生 `sign_wg/kps_wg/vcode`。因此本次结果证明手机扫码登录和浏览器侧协议成功，不能据此声称 Cookie 自动代确认、买家自动激活或第三方会话 24 小时远程注销已完成。

交付的 JSON 仅保留接口、参数名称、响应字段和状态；不包含 Cookie、二维码 token、Service Ticket、mobilekps、账号身份或文件信息。原始抓包仅在私有工作目录中以 AES-GCM 加密保存。


---

以下为此前协议分析与独立失败测试记录。

# 夸克官方扫码确认协议核对

核对日期：2026-10-10。来源是二维码短链实际跳转的官方页面及其公开脚本，不是猜测接口名。脚本 URL 和 SHA-256 记录于 `source-manifest.json`。

官方短链 `https://su.quark.cn/4_eMHBJ` 跳转至 `https://b.quark.cn/apps/DuJrlezmS/routes/EATJRLSvv`。页面配置的服务端是 `https://uop.quark.cn`。

点击确认时，页面向 App 原生桥请求账号签名，夸克分支为 `account.getUserInfo`，UC 分支为 `account.getSignedUserInfo`，传入 `vCode`（当前毫秒时间）。返回成功且含 `sign_wg`、`kps_wg` 才继续；否则打开 App 登录窗口。普通网页 Cookie 不会被当作这些参数读取。

真实确认请求：`POST https://uop.quark.cn/cas/ajax/loginWithKpsAndQrcodeToken`，`Content-Type: application/x-www-form-urlencoded`。字段为 `client_id=532`、`v=1.2`、`request_id`、二维码 `token`、`sign_wg`、`kps_wg` 和 `vcode`。官方业务状态 `2000000` 表示确认请求成功；随后仍须核对买家会话。

状态轮询：`GET https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken`。仅业务状态成功且存在 Service Ticket 才表示授权已确认，不能只看 HTTP 200。签名不由本系统生成、不从 Cookie 猜测、不复用伪造值。当前本地桥允许签名时间码最多滞后两分钟、领先三十秒，这是本系统的 freshness 限制，不能解释成官方签名有效期说明。

## 已实现与缺失

`QuarkReferenceProvider` 按上述固定目的地址和表单字段实现确认与状态查询；管理员通过 `/api/admin/quark-cookie/confirm-qr` 提交实际 App 请求中的参数。参数不存盘、不进日志、不返给买家。Service Ticket 不回传、不进入审计。该入口仅用于真实接口联调，不创建 Lease，也未提供远程指定设备注销。

实际读请求：账号接口返回 HTTP 200，但 `success=false`、`code=AUTH_ERROR:50051`；会员接口返回 HTTP 401、业务码 31004。22 条导入 Cookie 已保存，当前请求未建立账号登录态。本机官方桌面客户端的账号管理页面也显示“请登录后再查看账号管理选项”。

当前未获得会员 App 的一次成功确认请求或原生签名输出，因此未向真实确认接口提交伪造参数，也不能声称 Cookie 自动确认实际登录成功。HTTP 请求结构和安全边界通过 MockTransport 测试；这些不是官方成功联调证据。

官方脚本来源：[扫码确认脚本](https://image.quark.cn/s/uae/g/1y/broccoli/DuJrlezmS/EATJRLSvv/bundle.752708baa310214926fb.js)。

## 纯网页版 + Cookie 的真实确认测试

用户指定只使用网页版，并明确授权查看官方跳转目的域名 h5.sm.cn。使用新申请的真实二维码，官方 b.quark.cn 确认页在普通浏览器自动转到 h5.sm.cn 的“立即下载”页面，没有提供确认登录按钮。已保存 quark-web-confirmation.png 作为实际页面证据。首次跳转因目的域名授权不足被自动审批拦截，取得用户明确授权后继续读取；没有绕过审批。

在已核实的官方确认接口提交现有完整 Cookie 和真实二维码 token，未猜测或填入 sign_wg/kps_wg：官方返回 HTTP 200，但业务状态 **50001**，未接受确认。随后真实轮询仍为 **50004001**，未出现 Service Ticket，没有建立买家登录会话。业务状态 50001 的具体含义未依据未验证文档猜测。

当前结果只证明本次网页路径和这份 Cookie 的实际确认未成功，不能扩大为平台所有可能的授权通道都不存在。实现自动确认仍缺官方接口要求的真实签名来源。
