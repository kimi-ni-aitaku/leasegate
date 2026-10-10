# QuarkPan / quark-drive 源码核对

核对日期：2026-10-10。固定提交和文件 SHA-256 见 [source-manifest.json](source-manifest.json)。QuarkPan 使用 `master`；其 `main` 文件地址未能读取，因此改为核对实际存在的分支。

## 确认存在的流程

- QuarkPan `APILogin.get_qr_code` 申请二维码 token，`_build_qr_url` 构造给用户扫描的地址；`check_login_status` 请求扫码状态。
- `wait_for_login` 等待用户操作，成功条件包括 Service Ticket；`_get_user_info_and_cookies` 使用 Ticket 获取登录 Cookie；`login` 返回收集到的 Cookie。
- `login.py` 的 `QuarkAuth` 实现已保存 Cookie 的读取、到期判断与保存。其 `logout` 清理本地 Cookie 文件；这不能证明远程注销指定设备的能力。
- quark-drive 的 `references/auth.md` 和 `scripts/quark_cli.py` 中 `QuarkQRLogin` 展示同样的申请、轮询、Ticket 兑换、Cookie 保存流程。状态码应按具体响应语义处理，不能只看 HTTP 200。

## 本项目采用与不采用的边界

采用授权任务状态机、超时、轮询间隔和服务端凭据生命周期的通用设计。不复制参考项目的请求头伪装、抓包协议、明文 Cookie 保存或敏感 debug 日志。

这些社区源码不是我们获得第三方 API 授权的证据。在此次阅读的文件中，没有找到足以支持“使用另一个账号确认买家上传的外部客户端二维码”的接口，也没有找到可据以实现远程指定设备注销的正式授权文档。因此 `QuarkReferenceProvider` 明确返回 501。

来源：[QuarkPan api_login.py](https://github.com/lich0821/QuarkPan/blob/master/quark_client/auth/api_login.py)、[login.py](https://github.com/lich0821/QuarkPan/blob/master/quark_client/auth/login.py)、[quark-drive auth.md](https://github.com/imeiming/quark-drive/blob/main/references/auth.md)、[quark_cli.py](https://github.com/imeiming/quark-drive/blob/main/scripts/quark_cli.py)。固定版本链接见 manifest。

后续官方页面核对已找到 App 签名确认接口，见 [quark-official-confirmation.md](quark-official-confirmation.md)。初次社区源码未发现接口的结论不能扩大为平台没有该接口。当前接口可按真实协议调用，但需要 App 提供的真实签名；Cookie 本身未提供这些参数。
