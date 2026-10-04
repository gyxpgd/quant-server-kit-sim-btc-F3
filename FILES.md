# 文件清单与保留边界

| 文件或目录 | 用途 |
| --- | --- |
| `README.md` | 入口与运行边界 |
| `docs/DEPLOY.md` | 新装：代理、检查、首次模拟和用户服务 |
| `docs/OPERATIONS.md` | 状态、停止/恢复、导出、20U/60U 切换 |
| `docs/MIGRATE.md` | 旧系统级服务迁移到用户级，保留原实验 |
| `docs/PROXY.md` | 代理设置与现有 Mihomo 复用 |
| `docs/GITHUB.md` | 上传干净源码与编辑提交步骤 |
| `.env.example` | 可公开的本机代理环境示例；不是实际运行配置 |
| `.gitignore`、`.gitattributes` | 排除私有文件并保持Linux脚本换行 |
| `.github/workflows/offline-tests.yml` | GitHub Actions离线测试，无服务器部署 |
| `tools/check_repo.py` | 上传前文件检查，--staged检查准备提交的内容 |
| `tools/mihomo/` | 独立代理的辅助配置脚本与服务模板 |
| `STRATEGY.md` | F3 模型、成本、风控和公开行情模拟限制 |
| `paper.py` | `check`、`run`、`status`、`export` 命令及运行循环 |
| `paper_engine.py` | 增量模拟账本、仓位和收益计算 |
| `public_data.py` | 公开 GET 接口、时间/行情/资金费与规格校验 |
| `store.py` | SQLite 事务、身份校验、进程锁和一致性快照 |
| `reference_engine.py`、`signals.py`、`common.py` | 原 F3 参考引擎、信号和公共计算 |
| `config-F3-20u.json`、`config-F3-60u.json` | 冻结的虚拟 20U、60U 配置 |
| `deploy/install-service.sh` | 生成用户服务，仅安装并 `daemon-reload` |
| `deploy/okx-funding-f3.service.in` | 服务模板，不能直接复制成最终 unit |
| `source-sha256.txt` | 七个核心文件和两份配置的校验值，迁移前在旧目录检查 |
| `tests/` | 无网络程序测试 |
| `validation/` | 随原包保留的历史验证与来源记录，不代表本次服务器验收 |
| `TEST-REPORT.md`、`VERSION.json`、`release-manifest.json` | 本次发布说明、验证范围与文件哈希 |

## 本地生成，不上传 GitHub

| 路径 | 保留要求 |
| --- | --- |
| `.env` | 后台代理环境；权限 `600`，不放 API Key；终端不会自动读取 |
| `var/20u/`、`var/60u/` | 每个实验独立的账本、行情、事件、检查点和日志 |
| `var/*/paper.sqlite3` 及相邻 SQLite 文件 | 模拟账本，不能删库重置；运行时使用 `export` 做一致性快照 |
| `var/*/process.lock` | 单数据目录的进程锁，不能删锁重复启动 |
| `var/*/run.log*` | 轮转日志，含实验运行信息 |
| `export-*.zip` | 用户自己的账本导出 |
| `~/.config/systemd/user/okx-funding-f3.service` | 安装后生成的个人用户 unit，记录绝对路径 |
| 原系统 unit 备份与项目备份 | 迁移恢复材料，保存在受控位置 |

七个核心文件是 `paper.py`、`paper_engine.py`、`public_data.py`、`store.py`、`reference_engine.py`、`signals.py`、`common.py`。账本将它们的 SHA-256 与完整运行配置绑定；修改其中任何一个后不能把结果静默接入原账本。

本次用户级服务改造只改变部署方式，迁移时保留原七个核心文件、配置、`.env` 和 `var/`。服务模板、文档和 `.env` 不属于上述源码身份，但更改代理仍会影响数据连通。服务支持一个 20U 或 60U 账户；配置和数据目录必须配对。
