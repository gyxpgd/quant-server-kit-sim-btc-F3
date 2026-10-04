# F3 资金费率策略 · 公开行情模拟

个人用户服务版 `F3-paper-1-user-service.1` · 2026-10-04

在自己的 Linux 用户目录运行 F3，读取 OKX 公开行情，在本机记录 BTC 现货与 BTC-USD 币本位永续空头的**虚拟账户**。默认虚拟本金 20U，另附独立 60U 配置。

**无需充值、交易所账户或 API 密钥。程序不下单；它不是 OKX 官方模拟交易，也不是实盘程序。** `observed` 和 `recovered` 都是本机模型计算结果，不是交易所撮合成交。

本次发布将后台部署改为个人用户的 systemd 服务。七个核心 Python 文件、20U/60U 配置与原 F3 包一致；不会因下载、解压或保存到 GitHub 而更改你正在运行的服务器。发布目录不包含你的 `.env`、账本或代理订阅。

## 怎么开始

| 你的情况 | 阅读内容 |
| --- | --- |
| 服务器还没部署过 F3 | [首次部署](docs/DEPLOY.md)，以 20U 为默认流程 |
| 已有 Mihomo 或需要配置代理 | [代理教程](docs/PROXY.md)，区分终端环境与后台 `.env` |
| 原来使用系统级 F3 服务 | 先读[迁移说明](docs/MIGRATE.md)，保留原账本和程序目录 |
| 已经运行，要查看/停止/导出 | [日常操作](docs/OPERATIONS.md) |
| 上传 GitHub 或以后修改源码 | [GitHub 上传说明](docs/GITHUB.md) |
| 想了解模型和文件 | [策略规则](STRATEGY.md)、[文件清单](FILES.md)、[测试报告](TEST-REPORT.md) |

## 固定内容

| 项目 | 设置 |
| --- | --- |
| 程序模式 | `public-paper`，仅公开接口 GET |
| 模型标的 | BTC-USDT 现货 + BTC-USD-SWAP 币本位永续空头 |
| 默认账户 | `config-F3-20u.json` + `var/20u/` |
| 可选账户 | `config-F3-60u.json` + `var/60u/` |
| 信号评估 | 每天北京时间 09:00；公开资金费历史预热 16 天 |
| 默认轮询 | 30 秒；至少等待 2 分钟确认余量后推进模型 |
| 后台服务 | `systemctl --user` 管理的 `okx-funding-f3.service` |
| 环境 | Linux、Python ≥3.10、有效 CA 证书、用户级 systemd；需保持后台运行时启用 linger |

程序只用 Python 标准库，无需 pip 或 Docker。安装脚本只安装用户服务并执行 `daemon-reload`，不启动、不设置自启。日常管理不用 sudo；缺少系统依赖或无权开启 linger 时，需要管理员先完成一次环境准备。

F3 与 v53 的目录、账本、服务分别管理；可以复用已经可用的 Mihomo。不要为了试验 F3 断网恢复而停止实盘共用的代理。

## 常用查看

已按教程部署后，在服务器终端执行：

```bash
cd "$HOME/okx-funding-f3-paper"
python3 paper.py status --config config-F3-20u.json --data-dir var/20u
systemctl --user status okx-funding-f3.service --no-pager
journalctl --user -u okx-funding-f3.service -n 80 --no-pager
```

`active` 只表示进程运行。还要看“行情推进至”“最近检查”、错误状态、空仓/持仓和 `recovered` 分钟数。没有开仓可能只是尚未到评估时间或条件不满足。历史验证结果与本次发布检查见[测试报告](TEST-REPORT.md)，不能据此推断真实交易收益。
