# AList / OpenList Provider 模式核对

核对日期：2026-10-10。两仓库的 `drivers/quark_uc/` 与 `drivers/quark_uc_tv/` 实际阅读文件、固定提交、哈希记录于 [source-manifest.json](source-manifest.json)。

## 确认的结构

- 普通 Quark/UC Driver 接受 Cookie 配置，封装独立的请求客户端、文件操作、响应错误处理和 Cookie 生命周期。Driver 初始化、配置与销毁各有职责。
- TV Driver 的 `getLoginCode` 取得二维码资料和 QueryToken，保存后交给 `getCode` 查询用户确认产生的 Code。
- `getRefreshTokenByTV` 按首次 Code 或已有 RefreshToken 分支换取凭据。RefreshToken 更新后保存配置；AccessToken 供请求客户端使用。
- DeviceID 参与请求参数和签名相关上下文。该上下文不同于可任意注销的远程设备管理 API。
- TV 请求层会识别凭据失效并进入刷新路径。生产实现应限定重试次数，避免递归重试失控。

## 抽象进入本系统的内容

Provider 接口独立；官方 Device Grant 使用明确配置的 issuer 和 HTTPS 端点；授权任务与 Lease 分开；长期凭据 AES-GCM 加密保存于服务端；本系统撤销与第三方远程撤销分别记录。

源码内路径或字段名称带有 OAuth，不代表它是运营者可直接使用的开放 OAuth 产品。我们没有复制内置平台签名配置，也没有接入这些社区协议。实际 OAuthAdapter 使用 [RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)，只接受运营者明确配置且获得授权的客户端。

来源：[OpenList TV util.go](https://github.com/OpenListTeam/OpenList/blob/main/drivers/quark_uc_tv/util.go)、[OpenList Quark/UC util.go](https://github.com/OpenListTeam/OpenList/blob/main/drivers/quark_uc/util.go)、[AList TV util.go](https://github.com/AlistGo/alist/blob/main/drivers/quark_uc_tv/util.go)、[AList Quark/UC Driver](https://github.com/AlistGo/alist/blob/main/drivers/quark_uc/driver.go)。
