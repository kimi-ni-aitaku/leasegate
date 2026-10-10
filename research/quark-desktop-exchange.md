# 官方网页到客户端授权交换实测

核对日期：2026-10-10。此记录补充扫码确认和 Cookie 失败结果，不代表买家自动登录成功。

来源为用户此前授权的成功手机登录抓包内的官方公开网页脚本：[cloud-drive-web 4.6.7/index.js](https://g.alicdn.com/uc-cloud-drive-web-system/cloud-drive-web/4.6.7/index.js)。仅分析代码，没有执行下载脚本。官方网页用 `account/info` 返回的 `mobilekps` 作为 `kps_wg`，结合本机客户端的 `sessionDevice`，请求客户端 App Token；这与扫码确认需要的 `sign_wg/kps_wg/vcode` 是不同步骤。

| 步骤 | 已核对的官方请求 | 实测结果 |
| --- | --- | --- |
| 查询客户端 | 本机 `/desktop_info`，官方源码端口 9125–9127 | 9125 可用，`isLogin=false`，夸克网盘版本 `7.3.5.810` |
| 读取设备凭据 | 本机 `/desktop_devicetoken` | `success=true`，设备字段位于 `data.data.sessionDevice`；值不导出 |
| 请求 App Token | `POST https://uop.quark.cn/cas/ajax/acquireAppToken?client_id=386&v=1.2` | HTTP 200，业务码 `50051`；未返回 App Token |
| 写入客户端会话 | 官方脚本使用本机 `/desktop_postlogin` | 未执行：前一步未颁发 App Token，不能构造成功会话 |

官方 Token 请求是表单：`kps_wg`、`session_device`、`v=1.2`、`client_id=386`、随机 `request_id`。本次使用用户此前授权的成功账号响应中的真实 `mobilekps`；未猜测签名、未复用历史 Service Ticket、未向买家浏览器注入会员 Cookie。该凭据当前交换被拒绝，不能把此前手机登录成功推断成现在仍有效。

当前有两项待解决：有效会员账号凭据，以及扫码确认的原生签名来源。客户端 Token 交换成功本身也不能证明扫码确认签名可以获得。没有验证这两项前，不启用自动确认或颁发表示夸克登录成功的 Lease。默认人工模式关闭。

对应脱敏结果见 [quark-desktop-exchange.json](quark-desktop-exchange.json)。所有 Cookie、账号身份、mobilekps、sessionDevice、原始 HAR 和运行密钥均不上传 GitHub。
