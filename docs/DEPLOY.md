# 首次部署：个人用户版

适用于全新的 F3 安装。已有程序、账本或系统级 `okx-funding-f3.service` 时，先读[迁移说明](MIGRATE.md)，不要覆盖原目录、删库或重新开始实验。本页所有程序和服务命令由自己的普通 Linux 用户执行。

## 1. 登录并检查环境

在电脑本地终端填入自己的信息：

```bash
SERVER='你的服务器地址'
SERVER_USER='你的Linux用户名'
ssh "${SERVER_USER}@${SERVER}"
```

此后命令在服务器终端执行：

```bash
python3 --version
python3 -c 'import ssl, sqlite3; print(ssl.get_default_verify_paths())'
systemctl --user show-environment >/dev/null
loginctl show-user "$(id -un)" -p Linger
timedatectl status
```

需要 Python **3.10 或更高版本**、CA 证书、运行中的用户级 systemd；下载方式还需要 Git 或 unzip。只用 Python 标准库，无需安装 pip 包。系统时间应准确并保持同步；程序内部按北京时间计算，不要求改变整台主机的时区。

若 `Linger=no`，先尝试：

```bash
loginctl enable-linger "$(id -un)"
```

再次查看，确认 `Linger=yes`。若权限不足，或缺少 Python/证书/用户服务环境，需要管理员一次协助。普通用户不能保证无权限安装系统软件或更改主机策略；在环境备好后，后续安装和日常管理均无需 sudo。

## 2. 下载完整目录

先看目标目录是否已经存在：

```bash
ls -ld "$HOME/okx-funding-f3-paper"
```

只有显示目录不存在时才新装。下面两种下载方式**选一种**。

**方式 A：GitHub 克隆。** 仓库已发布后，用自己的 GitHub 用户名替换占位符：

```bash
cd "$HOME"
git clone 'https://github.com/YOUR_GITHUB_NAME/okx-funding-f3-paper.git' okx-funding-f3-paper
cd "$HOME/okx-funding-f3-paper"
```

私有仓库需要你自己的 GitHub 登录或 SSH 授权；不要把 Token、密码或其他凭据写在仓库 URL 里。

**方式 B：上传本次发布 ZIP。** 将 `okx-funding-f3-paper-github.zip` 下载到 Mac 桌面，在 Mac 的另一个**本地终端窗口**执行，先替换用户名和服务器地址：

```bash
SERVER='你的服务器地址'
SERVER_USER='你的Linux用户名'
scp "$HOME/Desktop/okx-funding-f3-paper-github.zip" "${SERVER_USER}@${SERVER}:~/"
```

回到服务器终端。下面代码先检查目录不存在，才解压；若检查失败立即停止，不覆盖旧目录：

```bash
(
  set -eu
  test ! -e "$HOME/okx-funding-f3-paper"
  test ! -L "$HOME/okx-funding-f3-paper"
  unzip "$HOME/okx-funding-f3-paper-github.zip" -d "$HOME"
)
cd "$HOME/okx-funding-f3-paper"
```

上面针对本次提供的发布 ZIP。GitHub 网页的“Download ZIP”顶层名称可能带分支名；使用那种 ZIP 时先在独立位置解压，再把完整项目目录放到尚不存在的 `~/okx-funding-f3-paper`，不能覆盖已有安装。

确认能看到：

```text
paper.py
config-F3-20u.json
config-F3-60u.json
deploy/install-service.sh
```

服务安装会记录程序和 Python 的绝对路径，安装后不要随意移动项目目录。

## 3. 复用代理，并分别设置终端与后台环境

完整过程见[代理教程](PROXY.md)。已有同用户 Mihomo 时，先确认服务：

```bash
systemctl --user is-active mihomo-proxy.service
```

以下示例使用服务器本机已验证的 HTTP/mixed 入口 `http://127.0.0.1:17890`。Mac 的代理、SSH 和 Tailscale 不会自动代理服务器上的请求。

让**当前终端**中的检查和试跑使用代理：

```bash
export http_proxy='http://127.0.0.1:17890'
export https_proxy='http://127.0.0.1:17890'
export no_proxy='localhost,127.0.0.1,::1'
```

让**后台服务**读取代理，在项目目录创建 `.env`。下面命令拒绝覆盖已有文件：

```bash
(
  set -eu
  umask 077
  set -C
  cat > .env <<'EOF'
http_proxy=http://127.0.0.1:17890
https_proxy=http://127.0.0.1:17890
no_proxy=localhost,127.0.0.1,::1
EOF
  chmod 600 .env
)
```

已有 `.env` 时保留它，用 `nano .env` 核对后设置 `chmod 600 .env`；没有编辑器可请管理员预先准备。文件只写 `名称=值`，不写 `export`、API Key 或机场订阅。

**`paper.py` 不会自动读取 `.env`。** 终端里的 `export` 只影响该终端；用户服务通过 `EnvironmentFile` 读取 `.env`，不会自动继承 SSH 的 `export`。每次开新终端做联网检查，要重新设置上述变量。已确认服务器可直连时可省略代理和 `.env`，但要留意已有代理环境变量是否仍生效。

## 4. 检查公开数据并试跑 20U

```bash
python3 paper.py check --config config-F3-20u.json --data-dir var/20u
python3 paper.py run --config config-F3-20u.json --data-dir var/20u --once
python3 paper.py status --config config-F3-20u.json --data-dir var/20u
```

每一步成功后再继续：

- `check` 核对交易所时间、现货/永续/标记价、已结算资金费和交易规格，不创建模拟账本或交易所账户。
- `run --once` 获取数据并处理一轮；新目录首次运行会创建本机账本，已有账本则校验身份并接续。它不会清零已有实验。
- `status` 读取本地记录，不探测实时网络。请同时看状态和时间，不能只看余额。

首次获取之前 16 天已结算资金费做信号预热，不回填这 16 天的盈利。账户从新的分钟边界开始，至少等待 2 分钟确认余量；首次显示 `WAITING`、暂时没有净值明细可以正常。

需要前台连续观察时运行：

```bash
python3 paper.py run --config config-F3-20u.json --data-dir var/20u
```

另开服务器终端用 `status` 查看约 3—4 分钟后的推进情况。默认 30 秒轮询；若网络慢，时间会更长。按 `Ctrl+C` 结束前台后才能启动后台，不要同时运行两个 `run` 操作同一数据目录。

## 5. 安装用户服务，明确启动

```bash
bash deploy/install-service.sh 20u
```

安装脚本生成 `~/.config/systemd/user/okx-funding-f3.service`，执行用户级 `daemon-reload`，**不会启动，也不会 enable**。它拒绝覆盖运行中的、符号链接或内容不同的同名用户服务；检测到未妥善停用的旧系统服务时也会拒绝，按[迁移说明](MIGRATE.md)处理。

核对安装输出的用户、项目、账户选择和数据目录，再启动：

```bash
systemctl --user enable okx-funding-f3.service
systemctl --user start okx-funding-f3.service
systemctl --user status okx-funding-f3.service --no-pager
python3 paper.py status --config config-F3-20u.json --data-dir var/20u
journalctl --user -u okx-funding-f3.service -n 80 --no-pager
```

`enable` 设置用户服务自启，`start` 启动本次运行。配合 `Linger=yes`，退出 SSH 后和主机重新开机后可以继续运行。Linux 仍须通电、联网且不休眠。

观察最近检查时间与行情推进时间持续前进、无 `ERROR`，再退出 SSH。每天北京时间 09:00 才评估入场条件，空仓不等于程序故障；不要改时钟、阈值或伪造行情来促成交易。后续查看、导出、60U 切换见[日常操作](OPERATIONS.md)。

个人用户服务与自启的系统行为可参考 [systemctl 官方文档](https://github.com/systemd/systemd/blob/main/man/systemctl.xml) 和 [loginctl 的 lingering 说明](https://github.com/systemd/systemd/blob/main/man/loginctl.xml)。本教程的具体路径和脚本行为以本仓库实现为准。
