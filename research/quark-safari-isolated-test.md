# Chrome 保持登录，Safari 独立二维码测试

核对日期：2026-10-11（Asia/Shanghai）。本次按用户要求保留 Chrome 作为会员账号来源，在 Safari 新标签页打开夸克官方网盘登录页。没有执行 Chrome 退出，没有向 Safari 注入会员 Cookie。

从 Chrome 开发者工具当前选中的完整请求 Cookie 加密更新本地凭据，官方账号接口返回 `success=true, code=OK`，官方会员接口返回 `member_type=SUPER_VIP`。后台账号池同步更新并检查，状态为 `web_only`：网页登录有效，扫码授权库存仍未启用。

Safari 页面实际生成 `client_id=532` 的二维码，通过本机截图解码取得，二维码原文只加密保存在私有目录。首次截图的二维码已经过期，未向确认接口提交；随后刷新 Safari，立即使用新二维码测试。

| 核对项 | 实际结果 |
| --- | --- |
| 新二维码确认前 | 官方轮询 `50004001`，`WAIT_USER` |
| 仅 Cookie 确认 | 官方 `loginWithKpsAndQrcodeToken` 返回业务状态 `50001` |
| 确认后轮询 | 仍为 `50004001`，`WAIT_USER`，未取得授权票据 |
| 确认后源 Cookie | 账号检查 `OK`，会员 `SUPER_VIP`，继续有效 |
| Safari 可见页面 | 仍显示扫码登录页，没有进入文件页面 |

提交仅含已核对的 `client_id`、`v`、`request_id`、实际二维码 token 与有效 Cookie；没有猜测 `sign_wg`、`kps_wg`、`vcode`。所有请求均为固定的夸克官方 HTTPS 接口，不跟随跳转。

本次排除了“测试时源 Cookie 已失效”这一条件，证明这份有效 Cookie 单独确认这个 Safari 二维码仍未成功。不能据此宣称所有可能的 Cookie 授权通道都不存在，也不猜测业务码 `50001` 的具体含义。二维码自动确认仍缺经过验证的授权能力；已成功的 Web → Desktop App Token 交换属于另一条路径。

对应脱敏结果见 [quark-safari-isolated-test.json](quark-safari-isolated-test.json)。Cookie、二维码 token、账号与设备身份、运行密钥未进入源码或交付包。
