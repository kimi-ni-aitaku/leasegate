# 新 Cookie 与电脑客户端交换联调

核对日期：2026-10-11。所有凭据、账号身份和设备凭据仅在私有工作目录加密保存，没有进入源码或此报告。

用户提供的新 Cookie 最初检查成功：夸克 `account/info` 返回 `success=true, code=OK`，官方会员接口返回 `member_type=SUPER_VIP`。因此，旧 Cookie 的失败记录不能作为新凭据失败的证据。

按照[夸克官方网页脚本](https://g.alicdn.com/uc-cloud-drive-web-system/cloud-drive-web/4.6.7/index.js)的电脑授权流程，读取本机官方客户端真实 `sessionDevice`，以已认证账号接口返回的 `mobilekps` 请求 `acquireAppToken`。本次返回业务状态 **20000**，取得 `data.app_token`。这个成功码属于客户端 Token 交换，不是二维码确认接口的成功码。

本机 `desktop_devicetoken` 每次读取会刷新挑战；首次复核发现挑战变化后没有交付旧 Token。随后使用新挑战再次兑换并立即向原本机的 `desktop_postlogin` 交付；本机返回 HTTP 200、`success=true`。客户端显示账号的“确认登录”窗口，页面同时显示用户协议和隐私政策。**未点击该按钮；`desktop_info.isLogin` 仍为 false，实际电脑登录尚未验证成功。**

用户随后说明网页版已退出。再次只读检查已保存 Cookie，账号接口返回 `AUTH_ERROR:50051`，会员接口 HTTP 401。可确认该 Cookie 当前不可用；没有证据将退出原因归结为某个具体操作。

本次证据支持：有效网页会话可以取得官方设备绑定的电脑版 Token。这条路径独立于 `client_id=532` 的二维码代确认；未取得原生扫码签名，没有验证自动确认、远程买家登录或到期自动注销。现有 `VIPGateway` 仍需真实授权端。

已把验证过的交换流程封装为 `app/providers/quark_desktop.py` 与管理员本机工具 `python -m scripts.quark_desktop`。工具拒绝覆盖已登录的客户端、拒绝跳转、限制响应体、不会重试授权请求，不打印 Token，不点击官方协议。它是本机联调工具，尚未接入买家卡密的自助 PC 登录。
