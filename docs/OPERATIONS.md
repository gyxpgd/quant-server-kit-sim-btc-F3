# 日常操作、导出与恢复

以下命令在服务器的普通用户终端运行，默认目录 `~/okx-funding-f3-paper`、20U 配置与账本。F3 只做本机模拟，没有 API 密钥、`arm`、真实订单或手动平仓命令。

## 查看进程和数据是否正常

```bash
cd "$HOME/okx-funding-f3-paper"
python3 paper.py status --config config-F3-20u.json --data-dir var/20u
systemctl --user status okx-funding-f3.service --no-pager
journalctl --user -u okx-funding-f3.service -n 80 --no-pager
```

连续看日志：

```bash
journalctl --user -u okx-funding-f3.service -f
```

按 `Ctrl+C` 只退出日志显示，不停止后台。程序也在 `var/20u/run.log` 保存轮转日志。

| 状态或字段 | 如何理解 |
| --- | --- |
| `WAITING` | 等待已确认行情与结算余量；新账户刚启动时常见 |
| `OK` | 最近一轮已推进到本轮允许处理的边界，仍要核对时间 |
| `CATCHING_UP` | 正在分段补算历史缺口，不是实时交易 |
| `ERROR` | 网络、数据或账本校验失败，本轮停止推进；保留报错排查 |
| 超过 3 分钟未更新 | 最近检查记录已经过期；查看进程、日志和网络 |
| 最近检查 | 最近一次处理尝试的时间；错误重试也会更新时间，不代表成功推进 |
| 行情推进至 | 模型处理到的时间；默认落后当前时间至少约 2 分钟，留出确认余量 |
| 空仓 | 尚未到评估时间、信号不满足、退出或风控停止均可能造成，要结合日志判断 |
| 账户风控停止 | 模型停止新增交易；不能删除账本或修改阈值来清除 |
| 资金费累计 | 包括未平仓组已到账资金费，尚未扣除全部成本，不等于净利润 |

`status` 读取本地快照，不联网确认。服务 `active`、最近检查很新、甚至保留的状态写着 `OK`，都不能单独证明当前网络与数据正常；要看行情时间持续推进。

## 停止、继续、重启

按需要选择一条，不必连着执行：

```bash
systemctl --user stop okx-funding-f3.service
systemctl --user start okx-funding-f3.service
systemctl --user restart okx-funding-f3.service
```

停止进程会保留模拟持仓和待执行动作，不会清零账户或按市价模拟平仓。重新启动读取原账本，可能补算停机期间历史。它不是“这段时间永远不算”的暂停功能。

长期停用并取消自启：

```bash
systemctl --user disable --now okx-funding-f3.service
```

恢复自启和运行：

```bash
systemctl --user enable --now okx-funding-f3.service
```

不要在服务运行时再开前台 `run` 或 `run --once`。同一数据目录有进程锁，报冲突时先核对已有进程，不要删除锁文件。

## 联网检查与修改代理

后台 `.env` 不会自动加载进你新开的终端。做公开数据检查时，先按实际代理设置当前 shell，例如：

```bash
export http_proxy='http://127.0.0.1:17890'
export https_proxy='http://127.0.0.1:17890'
export no_proxy='localhost,127.0.0.1,::1'
python3 paper.py check --config config-F3-20u.json --data-dir var/20u
```

`check` 不初始化账本，可在后台运行时做一次诊断。具体代理排查见[代理教程](PROXY.md)。修改 `.env` 后，需要 `systemctl --user restart okx-funding-f3.service` 才让后台进程重新读取。不要关掉 TLS 校验或随意换数据域名。

如果 F3 与实盘共用 Mihomo，不要为模拟故障测试停止代理、切节点或断开网络；这些动作会影响实盘。需要故障注入时使用独立测试环境。

## 断网与重启恢复

数据不完整或校验失败时停止推进，连续运行模式会等待重试；恢复后从原账本接续。每次最多补算 6 小时，较大缺口可能需要多轮。

- `observed`：及时采集已确认 K 线后计算的模型结果。
- `recovered`：延迟采集后补算的历史模型结果，不能说成当时在线完成的交易。

两种分类都不是 OKX 订单。默认停机补算上限为 30 天；超过上限、行情被修订、迟到资金费或代码/配置身份不一致时，保留账本与错误并导出检查。确需另做实验时用明确的新数据目录，旧结果必须保留并分别标记，不把新实验冒充原实验续接。

需要验证退出 SSH 后继续运行，可正常退出并重新登录，检查用户服务和数据时间。整机重启会影响同机实盘与其他服务，另行安排维护；本教程不要求为验收立即重启服务器。

## 导出和备份

导出采用 SQLite 一致性快照，可以在服务运行时执行；下面使用时间戳避免覆盖旧导出：

```bash
cd "$HOME/okx-funding-f3-paper"
f3_export="export-20u-$(date +%Y%m%d-%H%M%S).zip"
python3 paper.py export --config config-F3-20u.json --data-dir var/20u --output "$f3_export"
```

文件包含账本快照、状态、绑定配置和源码身份，不包含 `.env`。程序拒绝覆盖已存在的输出文件。导出中仍有你的实验记录，应保存在你控制的位置。

在电脑本地终端取回，替换变量和实际导出文件名：

```bash
SERVER='你的服务器地址'
SERVER_USER='你的Linux用户名'
F3_EXPORT='刚才生成的export-20u-时间戳.zip'
scp "${SERVER_USER}@${SERVER}:~/okx-funding-f3-paper/${F3_EXPORT}" "$HOME/Downloads/"
```

完整迁移备份还需保留原 `.env`、配置和七个核心源码。文件级复制整个 `var/` 前先停止进程；不要在写入中只复制单个 `.sqlite3`，也不要单独删除 `-wal`、`-shm` 文件。

## 可选：将唯一后台服务切换为 60U

`20u` 与 `60u` 是两个独立虚拟账户，分别配对：

| 配置 | 数据目录 |
| --- | --- |
| `config-F3-20u.json` | `var/20u` |
| `config-F3-60u.json` | `var/60u` |

后台模板只有一个 `okx-funding-f3.service`，此流程切换它运行的账户，不同时启动两套后台。先停止 20U，按上文设置当前终端代理，检查 60U；已有 60U 数据会续接，不清零：

```bash
systemctl --user stop okx-funding-f3.service
cd "$HOME/okx-funding-f3-paper"
python3 paper.py check --config config-F3-60u.json --data-dir var/60u
python3 paper.py run --config config-F3-60u.json --data-dir var/60u --once
python3 paper.py status --config config-F3-60u.json --data-dir var/60u
```

确认试跑成功后，原 20U unit 与新模板内容不同，安装脚本会拒绝覆盖。先保留旧 unit 再安装；以下只移动个人用户的服务文件，不动项目账本：

```bash
(
  set -eu
  systemctl --user disable okx-funding-f3.service
  f3_unit="$HOME/.config/systemd/user/okx-funding-f3.service"
  test -f "$f3_unit"
  test ! -L "$f3_unit"
  f3_unit_backup=$(mktemp -d "$HOME/f3-user-unit-backup.XXXXXX")
  mv -- "$f3_unit" "$f3_unit_backup/okx-funding-f3.service"
  systemctl --user daemon-reload
  bash deploy/install-service.sh 60u
)
```

若存在服务 drop-in，先人工核对其用途，不要删除它来绕过安装拒绝。安装成功、输出确认是 60U 后再启动：

```bash
systemctl --user enable --now okx-funding-f3.service
python3 paper.py status --config config-F3-60u.json --data-dir var/60u
```

`var/20u` 继续保留。60U 的查看、联网检查和导出都使用 60U 配置与目录；不要只改配置文件名而忘了数据目录。以后切回 20U 同样先停止、备份当前 unit、安装 `20u`，然后续接原 `var/20u`。
