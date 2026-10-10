# 仅网页版 Cookie 路线复核

核对日期：2026-10-10。用户明确要求只使用网页版 Cookie，不接入手机 App。当前没有验证成功的买家自动登录方案；本报告不把推测当作实现结果，也不证明平台永远不存在其他授权路径。

## OpenCLI 咨询结果

通过 OpenCLI 1.8.8 的 `chatgpt ask` 和 `gemini ask` 实际提交了脱敏技术问题，并分别追问。只提供已观察到的接口、错误码、公开源码地址与控制流，不提交 Cookie、账号身份、二维码 token、原始 HAR 或运行密钥。

ChatGPT 首轮把 `acquireAppToken → desktop_postlogin` 的方向理解反了。核对官方源码后，向其纠正；它承认这是已登录网页授权本机桌面客户端，不能用作桌面客户端批准买家二维码的证据。其后提出原生 App 自动化候选；该路线不符合本次用户约束，未采用。ChatGPT 未给出能够立即验证的 Cookie 单独确认路径。

Gemini 首轮没有提供方案；追问后给出凭据上下文与跨端流程分析，没有提供可运行的二维码确认方法。其对签名算法、硬件和错误码原因的推测，没有独立官方证据，未作为项目事实。

上述咨询只能帮助定位待核对的问题，不能替代官方源码和真实登录验证。

## 已核对的证据

| 项目 | 核对结果 | 证据边界 |
| --- | --- | --- |
| 官方二维码确认页 | `getSignedUserInfo()` 调用原生 `account.getUserInfo` 或 `account.getSignedUserInfo`，传入 `vCode`，要求返回 `sign_wg` 与 `kps_wg`；确认时将它们与 `vcode`、二维码 token 提交至 `loginWithKpsAndQrcodeToken` | 当前查看的代码中未发现普通 Web Cookie 的替代分支；修改浏览器 UA 或页面预览标志不能证明获得了真实凭据 |
| 普通浏览器入口 | 非原生容器且非预览模式时进入 `loginMiddlePage`；此前浏览器查看官方跳转页显示 App 引导 | 允许打开该页不等于它已具备原生账号桥 |
| Cookie 单独确认实测 | HTTP 200，业务码 `50001`，没有成功票据 | 未获得官方错误码定义，不能断言具体签名校验原因 |
| 保存的会员凭据 | 账号检查返回 `AUTH_ERROR:50051`，会员接口 HTTP 401 | 已保存或此前有效不等于当前被接口接受；不能仅凭错误码认定是过期、格式错误或权限问题 |
| 网页到桌面客户端交换 | `mobilekps + sessionDevice → acquireAppToken → desktop_postlogin`；本次交换返回 `50051`，没有 App Token | 是 Web → Desktop；没有调用缺少有效 Token 的下游登录接口 |
| 其他桌面桥函数 | 官方代码包含 `desktop_weblogin_confirm`、`desktop_st` 等函数名 | 尚未证明它们接受远端买家二维码 token，不能据名称接成自动确认接口 |
| OpenCLI 浏览器身份补查 | `quark whoami` 本次命令退出码 77，未取得可用身份结果 | 不用这次命令失败判断浏览器是否已登录 |

源码来自此前已授权抓包中的官方公开 JavaScript，分析时没有执行脚本。扫码页源码及 SHA-256 见 [source-manifest.json](source-manifest.json)；桌面交换的真实脱敏请求结果见 [quark-desktop-exchange.md](quark-desktop-exchange.md)。Cookie 单独确认记录见 [quark-uploaded-cookie-confirmation.md](quark-uploaded-cookie-confirmation.md)。

## 当前结论与成功标准

在只使用网页版 Cookie 的限制下，项目仍不能让买家上传其官网二维码后自动登录。恢复有效的会员 Web 会话只能解决身份基线问题，不能单独证明二维码确认所需凭据已经可用。没有发现可验证的替代路径之前，不启用自动确认，不复制会员 Cookie 给买家，不复用历史票据，不把本地 Lease 激活称作夸克登录成功。

要改变这一结论，必须取得适用于 Web 会话的真实官方确认流程或授权接口，并使用买家新生成的二维码，观察其原始浏览器获得新的 Service Ticket、建立登录态且会员身份检查成功。目前没有完成该验证。

本系统 24 小时到期仅撤销本系统 Lease 与资源权限；没有已验证的夸克指定设备会话撤销接口，因此不能承诺买家夸克会话在 24 小时后被自动注销。
