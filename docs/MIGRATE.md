# 从旧系统服务迁移到个人用户服务

本页用于已经运行旧 F3 的主机。**迁移只改由谁管理后台服务，保留原模拟实验。** 新包没有升级七个核心程序或 20U/60U 配置，不需要重新初始化账户，不需要删除数据。仅下载这份包不会操作你的服务器。

旧服务由 `systemctl` 管理，新服务由 `systemctl --user` 管理，名字都叫 `okx-funding-f3.service`。两者属于不同管理层；必须先妥善停用旧系统服务，才能启用个人用户服务，避免重复启动。

## 1. 保留原目录，在另一位置查看新包

将新包解压到独立的临时目录。例如：

```text
~/okx-funding-f3-paper/                       原运行目录，保持不动
~/f3-release-review/okx-funding-f3-paper/     新包完整解压目录
```

下面的路径需要与实际位置一致。登录原来拥有程序的普通用户，在服务器终端设置：

```bash
f3_existing="$HOME/okx-funding-f3-paper"
f3_release="$HOME/f3-release-review/okx-funding-f3-paper"
cd "$f3_existing"
ls -ld "$f3_existing" "$f3_existing/var"
python3 paper.py status --config config-F3-20u.json --data-dir var/20u
```

若原服务运行的是 60U，整页所有账户命令使用 `config-F3-60u.json`、`var/60u`，安装参数使用 `60u`。不要凭默认值改变正在进行的实验。

记录开始时间、行情推进时间、虚拟资金、持仓与数据目录。确认目录与运行数据属于当前普通用户；如果旧目录/数据由 root 或别的用户持有，先请管理员整理归属，不能用 root 启动新服务。

## 2. 管理员一次停用旧系统服务

这一步作用于**旧系统服务**，是迁移需要的一次管理员权限操作。由有权限的人执行：

```bash
sudo systemctl disable --now okx-funding-f3.service
```

然后只读确认：

```bash
systemctl show okx-funding-f3.service -p ActiveState -p UnitFileState -p TriggeredBy -p DropInPaths
```

原安装正常停用后应为 `ActiveState=inactive`、`UnitFileState=disabled`，并且没有仍可启动它的活动触发器。若状态仍为 active/activating，或有定时器等额外触发来源、服务 drop-in，请管理员先核清。新安装器也会检查旧系统服务；不明确时拒绝安装。不要删除系统文件或用两个管理层轮流强行启动。

从这一步之后，下面服务操作全部使用自己的 `systemctl --user`，不再使用 sudo。

## 3. 备份并核对七个核心文件及配置

确认旧进程停止后，保存原部署文件和运行数据。备份目录由 `mktemp` 创建为当前用户的私有目录；准备足够磁盘空间：

```bash
f3_backup=$(mktemp -d "$HOME/f3-migration-backup.XXXXXX")
cp -a "$f3_existing/deploy" "$f3_backup/deploy"
cp -a "$f3_existing/var" "$f3_backup/var"
if [ -f "$f3_existing/.env" ]; then
  cp -p "$f3_existing/.env" "$f3_backup/.env"
fi
```

原 `var/` 和 `.env` 仍留在原位置。不要单独删除 SQLite 的 `-wal`、`-shm` 文件；这里复制的是已停止写入的整个数据目录。

从新包取校验文件，在**旧目录**检查七个核心 Python 文件和两份配置是否完全一致：

```bash
cp -p "$f3_release/source-sha256.txt" "$f3_existing/source-sha256.txt"
cd "$f3_existing"
sha256sum -c source-sha256.txt
```

必须全部显示 `OK` 才继续。有一项不同就保留现场、先核对差异；不要覆盖核心、改哈希、删账本或换一个空数据目录来伪装迁移成功。该检查只证明文件一致，不能代替原账本身份校验。

## 4. 只更新部署文件，接续原账本

上一步全部通过后，只复制新包的两个部署文件：

```bash
cp -p "$f3_release/deploy/install-service.sh" "$f3_existing/deploy/install-service.sh"
cp -p "$f3_release/deploy/okx-funding-f3.service.in" "$f3_existing/deploy/okx-funding-f3.service.in"
cd "$f3_existing"
loginctl show-user "$(id -un)" -p Linger
```

若 `Linger=no`，先尝试 `loginctl enable-linger "$(id -un)"`；无权设置时请管理员一次协助。确认用户级 systemd 可用：

```bash
systemctl --user show-environment >/dev/null
bash deploy/install-service.sh 20u
```

安装器只安装个人 unit 并执行 `daemon-reload`，不会启动或 enable。核对输出仍指向原项目目录和原 `var/20u`，账户选择仍是 20U；确认保留的 `.env` 正确且权限为 `600`。该文件会被新后台服务读取，程序自身不会主动读取它。

明确启动并设置个人用户自启：

```bash
systemctl --user enable okx-funding-f3.service
systemctl --user start okx-funding-f3.service
systemctl --user status okx-funding-f3.service --no-pager
python3 paper.py status --config config-F3-20u.json --data-dir var/20u
journalctl --user -u okx-funding-f3.service -n 80 --no-pager
```

**不要再走首次新建账户流程，也不要删除原 `var/`。** 这里由服务直接读取原账本接续；开始时间和已有记录应保留，停机期间补算可能显示 `recovered`。观察数轮，确认行情时间持续推进且无身份错误。

## 如果没有成功

保留备份、原账本和脱敏日志。先停止新的个人服务以防两个管理层同时运行：

```bash
systemctl --user disable --now okx-funding-f3.service
```

若需退回旧系统服务，由管理员确认新服务确实停止后再安排恢复；不要自行同时开启二者。没有必要清空模拟账户、重装代理或停止与实盘共用的 Mihomo。

迁移完成后的所有查看、停止、启动、导出和 60U 切换都按[日常操作](OPERATIONS.md)执行。无旧系统服务的新用户直接使用[首次部署](DEPLOY.md)，无需执行本页管理员步骤。
