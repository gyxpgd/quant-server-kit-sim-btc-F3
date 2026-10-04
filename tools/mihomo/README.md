# Mihomo 配置辅助工具

完整操作见 [代理安装、检查与切换节点](../../docs/PROXY.md)。F3 可复用服务器已经配置好的 Mihomo；不需要为每个量化程序再安装一个代理。

| 文件 | 用途 |
| --- | --- |
| `configure.py` | 读取本地 Clash/Mihomo 订阅，选择一个节点，生成候选配置。不是官方 Mihomo 程序，也不会启动代理或量化。 |
| `mihomo-proxy.service` | 用户后台服务模板；读取 `~/mihomo/bin/mihomo` 和 `~/mihomo/private/config.yaml`。 |
| `.gitignore` | 忽略本目录中可能误放的二进制、私有配置和日志。已经提交的秘密不会因此从 Git 历史消失。 |

`configure.py` 与服务模板从已有 `quant-server-kit/tools/mihomo` 原样复制，未修改。Mihomo 二进制应按指南另从官方获取；本仓库不打包二进制、真实订阅、节点凭据或日志。

**把工具复制到仓库外的 `~/mihomo/` 再运行。** 配置生成位置取决于脚本自身所在目录，不是终端当前目录；不要直接在仓库 `tools/mihomo` 下生成配置。

脚本要求订阅含内嵌 `proxies` 列表。只提供 `proxy-providers`、Base64 链接列表或网页的订阅不适用。它固定使用一个节点和 `127.0.0.1:17890`，不复制订阅中的节点组及分流规则，不自动订阅更新或故障换线。若已存在正式 `private/config.yaml`，会拒绝覆盖；切换步骤请按完整指南执行。

F3 手动命令通过 `http_proxy`、`https_proxy`、`no_proxy` 环境变量使用代理；后台用户服务从项目 `.env` 读取这些变量。`paper.py` 本身不会自动读取 `.env`。F3 是只访问公开行情的本地资金费率模拟，不需要欧易 API 密钥，也不会发送交易所订单。
