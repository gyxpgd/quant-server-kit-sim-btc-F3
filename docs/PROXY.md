# F3 网络代理：复用、配置与检查

F3 是只访问公开行情的本地资金费率模拟，不使用交易所 API 密钥，不会发送真实或欧易模拟盘订单。它通过 Python 的 HTTP(S) 环境变量使用代理。

代理独立放在 `~/mihomo`，由 `mihomo-proxy.service` 用户服务管理；F3 放在 `~/okx-funding-f3-paper`。优先复用已经可用的代理，**不要为了安装 F3 重装、降级或重启现有 Mihomo**。

已有代理直接看第 1 节。全新服务器才从第 2 节开始。普通用户可以完成项目目录操作和日常 `systemctl --user` 管理；缺少系统依赖或 lingering 权限时，才需要管理员一次性协助。

除明确标为“Mac 本地”的命令外，以下都在服务器的 SSH 终端执行。`$HOME` 在服务器代表该服务器用户的主目录，在 Mac 代表 Mac 用户的主目录。

## 1. 已有 Mihomo：让 F3 使用，并验证是否连通

### 1.1 检查代理进程与实际访问路径

先逐条检查，不停止也不重启代理：

```bash
systemctl --user is-active mihomo-proxy.service
ss -ltn 'sport = :17890'
```

`active` 表示进程在运行；监听 `127.0.0.1:17890` 表示服务器自己的代理入口已打开。这两项都不能证明海外节点已经成功连接欧易。

下面读取 F3 20U 配置中真正使用的 `runtime.base_url`，再通过指定代理访问该站点的公开时间接口。当前交付默认是 `https://www.okx.com`，不用另一个域名的成功结果代替 F3 的实际路径：

```bash
f3_origin=$(python3 -c 'import json; from pathlib import Path; r=json.loads((Path.home()/"okx-funding-f3-paper/config-F3-20u.json").read_text()); u=r["runtime"]["base_url"]; assert u in ("https://www.okx.com","https://openapi.okx.com"); print(u)') &&
curl --fail --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  --noproxy '' --proxy http://127.0.0.1:17890 \
  "$f3_origin/api/v5/public/time"
```

正常应返回 JSON，包含 `"code":"0"` 和 `data` 中的 `ts`。`--noproxy ''` 防止终端已有的 `NO_PROXY` 让本次检查绕过代理；它只影响这次 `curl`，不会改系统网络。

超时、TLS 错误或 HTTP 错误需要继续排查线路，不能用 `active` 当作联网成功。公开时间接口成功后，再做下一步完整 F3 检查。

### 1.2 手动运行命令：先导出环境变量

**`paper.py` 本身不会自动读取 `.env`。** 在当前 SSH 终端逐行执行：

```bash
export http_proxy=http://127.0.0.1:17890
export https_proxy=http://127.0.0.1:17890
export no_proxy=localhost,127.0.0.1,::1
```

作用如下：

| 变量 | 用途 |
| --- | --- |
| `http_proxy` | 告诉支持该变量的 HTTP 客户端，HTTP 请求经过这个代理。 |
| `https_proxy` | 告诉 HTTPS 客户端通过这个 HTTP 代理建立连接；写 `http://` 并不表示关闭 HTTPS 加密。 |
| `no_proxy` | 访问这些本机地址时直连；不会让欧易地址绕过代理。 |

`127.0.0.1` 是服务器自己，`17890` 是 Mihomo 监听的入口。它们不是机场海外节点地址，也不是订阅网址或 Tailscale 地址。Mac 的代理不会自动传入服务器。

导出变量后，在同一个终端执行完整检查：

```bash
python3 "$HOME/okx-funding-f3-paper/paper.py" check \
  --config "$HOME/okx-funding-f3-paper/config-F3-20u.json" \
  --data-dir "$HOME/okx-funding-f3-paper/var/20u"
```

`check` 检查 F3 所需的公开接口和行情结构，不会下单，也不启动长期运行服务。`--config` 明确检查 20U 配置。为与后续运行命令保持一致，这里也写出 `--data-dir`，但 `check` 本身不会读写该账本目录。

这些 `export` 只对当前终端及它启动的程序有效。退出 SSH 后，下次手动做联网检查要重新执行；它们不会自动修改已经运行的后台服务。

### 1.3 后台用户服务：在项目 `.env` 设置相同入口

本仓库提供的个人用户服务使用 `EnvironmentFile=-项目绝对路径/.env`。这是 **systemd 在启动 Python 前读取文件并传入环境变量**，不是 Python 自动加载 `.env`。前面的负号表示文件缺失时不会阻止服务启动，所以需要自己确认代理文件已经准备好，不能假定缺省也会走代理。

`.env` 不存在时可用下面命令创建。若已经存在会保留，不覆盖其它本机设置：

```bash
cd "$HOME/okx-funding-f3-paper"
if [ -e .env ] || [ -L .env ]; then
  printf '%s\n' '.env 已存在，保留原文件；请在本地确认下面三个代理变量。'
else
  (
    umask 077
    cat > .env <<'EOF'
http_proxy=http://127.0.0.1:17890
https_proxy=http://127.0.0.1:17890
no_proxy=localhost,127.0.0.1,::1
EOF
  )
fi
```

已有 `.env` 应包含上述三行，每行一个变量，不要写成一整行；不需要加 `export`，也不需要任何 API Key。请只在本地查看/编辑，不提交 GitHub。

首次安装 F3 用户服务按 [部署说明](DEPLOY.md) 操作。若用户服务已经安装，改好 `.env` 后只重启 **F3 服务**使它重新读入环境变量；无需重启已可用的 Mihomo：

```bash
systemctl --user restart okx-funding-f3.service
systemctl --user status okx-funding-f3.service --no-pager
```

该命令只适用于本仓库的个人用户服务。旧的系统级 F3 服务不会被它停止或更新；从旧服务迁移时先按部署说明处理，避免同时运行两个实例。

### 1.4 查看 F3 模拟状态

```bash
python3 "$HOME/okx-funding-f3-paper/paper.py" status \
  --config "$HOME/okx-funding-f3-paper/config-F3-20u.json" \
  --data-dir "$HOME/okx-funding-f3-paper/var/20u"
```

`status` 只按 `--data-dir` 读取已有模拟数据库；它不会重新读取策略配置，也不会即时访问欧易或测试代理。查看“最近检查”“行情推进至”是否继续更新；旧的 `OK` 记录不等于此刻仍能联网。即时连通性用前面的 `curl` 和 `paper.py check` 验证。

只要代理入口还是 `127.0.0.1:17890`，更换海外节点无需重新修改 F3 的代理变量。若共用该代理的其它程序涉及真钱交易，不要为了给 F3 做测试而停掉共用代理。

## 2. 全新服务器：安装 Mihomo 程序与公开工具

### 2.1 检查环境

已有 `~/mihomo`、已有代理服务，或 17890 端口已被占用时，先回到第 1 节核对现状，不重复覆盖。新的服务模板固定使用普通用户的 `~/mihomo`。

日常启动和检查代理、F3 使用普通用户，不需要 `sudo`。先检查：

```bash
uname -m
python3 -c 'import yaml; print("PyYAML 可用")'
command -v curl gzip ss
```

只有依赖缺失时，才由有权限的管理员一次性安装。没有管理员权限就请管理员完成，不要尝试绕过系统权限：

```bash
sudo apt update
sudo apt install -y python3 python3-yaml curl ca-certificates gzip iproute2 dbus-user-session libpam-systemd
python3 -c 'import yaml; print("PyYAML 可用")'
```

后面的下载适用于 `uname -m` 输出 `x86_64` 的服务器；输出 `aarch64`、`arm64` 时不能照用。`python3-yaml` 是读取 YAML 订阅的工具依赖，量化程序自身仍使用 Python 标准库。

刚安装用户会话组件后，若 `systemctl --user` 提示连接总线失败，退出 SSH，再重新登录。不要用 `sudo systemctl` 代替用户服务命令。

### 2.2 从仓库复制辅助工具到独立目录

**先进入已经下载好的 F3 仓库根目录**（通常是 `~/okx-funding-f3-paper`），确认这里有 `paper.py`、`config-F3-20u.json` 和 `tools/`。随后整组执行：

```bash
repo_dir="$PWD"
(
  set -eu
  test "$(uname -s)" = Linux
  test "$(uname -m)" = x86_64
  test "$(id -u)" != 0
  test -f "$repo_dir/tools/mihomo/configure.py"
  test -f "$repo_dir/tools/mihomo/mihomo-proxy.service"
  test ! -e "$HOME/mihomo"
  test ! -L "$HOME/mihomo"
  install -d -m 700 "$HOME/mihomo" "$HOME/mihomo/bin" "$HOME/mihomo/private"
  install -m 644 "$repo_dir/tools/mihomo/configure.py" "$HOME/mihomo/configure.py"
  install -m 644 "$repo_dir/tools/mihomo/mihomo-proxy.service" "$HOME/mihomo/mihomo-proxy.service"
)
```

`repo_dir` 只是暂存“这份仓库在哪里”，不改变 `$HOME`。脚本依据自身所在目录生成 `private/`，所以应运行 `~/mihomo/configure.py`，不能直接在仓库的 `tools/mihomo/` 中生成私有配置。

`configure.py` 是本项目的配置辅助脚本，不是官方 Mihomo 程序。Mihomo 二进制在下一步从官方另行下载。

### 2.3 下载并校验官方 Mihomo

这里固定使用 **v1.19.31 / Linux amd64 compatible** 作为可重复的安装示例。它的附件地址和 SHA-256 于 2026-10-04 对照 [官方版本页](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.31) 及 [官方附件列表](https://github.com/MetaCubeX/mihomo/releases/expanded_assets/v1.19.31) 核对；不表示它永远是最新版本，也不要求已有可用安装降级。其他架构或版本应按 [官方架构说明](https://wiki.metacubex.one/startup/faq/) 选择，并同时修改文件名和校验值。

在服务器下载：

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 180 --retry 2 \
  --output "$HOME/mihomo/bin/mihomo.gz" \
  'https://github.com/MetaCubeX/mihomo/releases/download/v1.19.31/mihomo-linux-amd64-compatible-v1.19.31.gz'
```

若服务器暂时不能访问 GitHub，可以在 **Mac 本地终端**下载同一个 **Linux 文件**，然后上传。先把引号里的 `SERVER_USER` 和 `SERVER_IP` 换成服务器登录用户名及地址：

```zsh
server_user='SERVER_USER'
server_ip='SERVER_IP'
curl --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 180 \
  --output "$HOME/Downloads/mihomo-linux-amd64-compatible-v1.19.31.gz" \
  'https://github.com/MetaCubeX/mihomo/releases/download/v1.19.31/mihomo-linux-amd64-compatible-v1.19.31.gz' &&
scp "$HOME/Downloads/mihomo-linux-amd64-compatible-v1.19.31.gz" \
  "${server_user}@${server_ip}:~/mihomo/bin/mihomo.gz"
```

下载或上传成功后，回到 **服务器**校验、解压；校验失败就停止：

```bash
(
  set -eu
  cd "$HOME/mihomo/bin"
  printf '%s\n' '04cf9f09671704f839ddbee2e93069dc831a4123a75281e725d1d96ab9ac1afc  mihomo.gz' | sha256sum --check -
  test ! -e mihomo
  test ! -L mihomo
  gzip --decompress --stdout mihomo.gz > mihomo.new
  chmod 755 mihomo.new
  mv mihomo.new mihomo
  ./mihomo -v
)
```

看到版本输出后继续。二进制、下载包均不提交 GitHub。

## 3. 在 Mac 下载私有订阅，再上传到服务器

订阅文件通常含节点服务器地址、端口以及接入凭据。它不是 Mihomo 程序，也不是量化代码；不要把订阅网址或文件发进聊天、GitHub 仓库和截图。

服务商需要提供 Clash/Mihomo 格式且有内嵌 `proxies` 节点列表的 YAML。只有 `proxy-providers`、通用 Base64 链接列表、网页或客户端专用加密配置不适用于此脚本。

在 **Mac 本地 zsh 终端**执行，出现提示后粘贴订阅链接并回车。输入不会显示；这不是卡住：

```zsh
read -r -s 'mihomo_sub_url?请粘贴 HTTPS 订阅链接，然后按回车：'
printf '\n'
(
  set -e
  umask 077
  curl --fail --location \
    --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 60 \
    --user-agent 'clash.meta' \
    --output "$HOME/Desktop/mihomo-subscription.download" \
    -- "$mihomo_sub_url"
  test -s "$HOME/Desktop/mihomo-subscription.download"
  mv "$HOME/Desktop/mihomo-subscription.download" "$HOME/Desktop/mihomo-subscription.yaml"
  chmod 600 "$HOME/Desktop/mihomo-subscription.yaml"
)
unset mihomo_sub_url
```

这里先下载到临时文件，HTTP 报错或空文件不会替换原来的 `.yaml`。成功会替换桌面同名订阅文件；如果你要同时保存多家订阅，请先用其他名字保存旧文件。该命令避免把明文链接写成终端历史中的命令，但不要开启 shell 调试输出，也不要分享运行中的进程详情。

仍在 **Mac 本地**上传；两处占位符需要换成自己的信息。此操作会更新服务器主目录下的同名订阅文件，但不会自动改变正在运行的节点：

```zsh
server_user='SERVER_USER'
server_ip='SERVER_IP'
scp "$HOME/Desktop/mihomo-subscription.yaml" "${server_user}@${server_ip}:~/"
```

回到 **服务器**收紧文件权限并确认文件存在，不打印内容：

```bash
chmod 600 "$HOME/mihomo-subscription.yaml"
ls -lh "$HOME/mihomo-subscription.yaml"
```

全新安装继续第 4 节；已有代理只换节点则跳到第 5 节。后续无需重复下载 Mihomo 程序。

## 4. 首次生成配置并启动后台服务

### 4.1 选择节点与检查配置

在服务器整组执行，并按提示输入节点序号。生成失败或取消就停止，不能把上一次留下的候选误当成新的配置：

```bash
(
  set -eu
  umask 077
  python3 "$HOME/mihomo/configure.py" "$HOME/mihomo-subscription.yaml"
  "$HOME/mihomo/bin/mihomo" -t \
    -d "$HOME/mihomo/private" \
    -f "$HOME/mihomo/private/config.candidate.yaml" \
    > "$HOME/mihomo/private/config-check.log" 2>&1
  test ! -e "$HOME/mihomo/private/config.yaml"
  test ! -L "$HOME/mihomo/private/config.yaml"
  mv "$HOME/mihomo/private/config.candidate.yaml" "$HOME/mihomo/private/config.yaml"
)
```

脚本的“需由安装脚本验证”是原版提示；本指南用 `mihomo -t` 完成这个验证。`-t` 验证配置格式及相关参数，不验证海外节点是否能联网。检查失败可在服务器本地看 `private/config-check.log`，其中可能含节点信息，不原样公开。

此辅助脚本固定生成单节点配置：入口 `127.0.0.1:17890`，不允许局域网接入，不开启 TUN、内置 DNS 和控制接口，不复制订阅的分流规则或节点组。所有进入该入口的请求使用所选节点，没有自动换线或自动更新订阅。

### 4.2 安装用户服务

只用于全新服务。下面发现已有服务文件、额外配置目录或端口占用时会停止，保留现状：

```bash
(
  set -eu
  test "$(id -u)" != 0
  if test -n "$(ss -H -ltn 'sport = :17890')"; then
    printf '%s\n' '17890 已被占用，先核对现有代理，不启动第二个服务。' >&2
    exit 1
  fi
  test ! -e "$HOME/.config/systemd/user/mihomo-proxy.service"
  test ! -L "$HOME/.config/systemd/user/mihomo-proxy.service"
  test ! -e "$HOME/.config/systemd/user/mihomo-proxy.service.d"
  systemd-analyze --user verify "$HOME/mihomo/mihomo-proxy.service"
  install -d -m 755 "$HOME/.config/systemd/user"
  install -m 644 "$HOME/mihomo/mihomo-proxy.service" "$HOME/.config/systemd/user/mihomo-proxy.service"
  systemctl --user daemon-reload
  systemctl --user enable --now mihomo-proxy.service
)
```

这段只安装个人用户服务，不调用管理员命令。若中途报错，已完成步骤不会自动撤销，先看服务文件和报错，不反复覆盖。

最后回到第 1 节做三层检查，再执行：

```bash
loginctl show-user "$(id -un)" -p Linger
```

预期 `Linger=yes`。如果已经是 `yes`，不要重复设置；如果为 `no`，请有权限的管理员一次性执行下列命令，为当前服务器用户允许开机和退出登录后继续运行用户服务：

```bash
sudo loginctl enable-linger "$(id -un)"
```

没有该权限时请管理员协助。未启用 lingering 前，不要把“退出 SSH、主机重启后持续运行”视为已经验收。日常 F3 与 Mihomo 管理仍使用 `systemctl --user`，不加 `sudo`。首次安装完成的结构为：

```text
~/mihomo-subscription.yaml     私有订阅，放在仓库外
~/mihomo/
├── configure.py              本项目配置辅助脚本
├── mihomo-proxy.service      服务模板
├── bin/mihomo                官方程序
└── private/
    ├── config.yaml           当前正式配置，含节点凭据
    ├── config-check.log      配置检查日志
    └── proxy.log             运行日志
```

## 5. 切换节点：保留旧配置，失败时恢复

**如果这个 Mihomo 同时被 F3、v53 或真钱量化使用，停止或切换它会同时影响所有这些程序。** 不要为了给 F3 测试断网而主动停止共用代理，也不要在 F3 安装时重启已经可用的代理。只有确实需要更换节点并已安排维护时才执行本节；涉及真钱程序时，先按该程序的管理说明核对实际持仓、委托与维护条件。暂停策略或停止进程都不等于平仓。F3 的故障测试应放在独立测试环境。

已上传的 `~/mihomo-subscription.yaml` 若仍有效，直接选节点即可；节点列表变了或更换服务商，先按第 3 节更新订阅。

`configure.py` 在正式 `config.yaml` 存在时会拒绝覆盖，因此下面会短暂停代理，先复制旧配置到备份再移除正式文件，然后生成候选。它会检查新配置、启动服务并测试欧易公开时间接口；任一步失败或取消，会尝试恢复原配置；此前运行则重启，否则保持停止。备份不自动删除。

**在服务器交互式 SSH 终端一次复制整段执行。** 使用 `</dev/tty` 让节点选择仍从键盘读取；不要将这段用于无人值守任务：

```bash
bash <<'BASH'
set -euo pipefail
umask 077
cd "$HOME/mihomo"
f3_origin=$(python3 -c 'import json; from pathlib import Path; r=json.loads((Path.home()/"okx-funding-f3-paper/config-F3-20u.json").read_text()); u=r["runtime"]["base_url"]; assert u in ("https://www.okx.com","https://openapi.okx.com"); print(u)')
test -t 1
test -f private/config.yaml
test ! -L private/config.yaml
test -f "$HOME/mihomo-subscription.yaml"
previous_state=$(systemctl --user is-active mihomo-proxy.service || true)
case "$previous_state" in
  active|inactive|failed) ;;
  *) printf '%s\n' '服务状态不明确，先查看 systemctl --user status mihomo-proxy.service。' >&2; exit 1 ;;
esac
backup="private/config.backup-$(date +%Y%m%d-%H%M%S)-$$.yaml"
switch_started=0
restore_on_exit() {
  result=$?
  trap - EXIT INT TERM
  if [ "$result" -ne 0 ] && [ "$switch_started" -eq 1 ]; then
    printf '%s\n' "切换未通过，尝试恢复旧配置；备份保留在：$HOME/mihomo/$backup" >&2
    if cp -p -- "$backup" private/config.yaml; then
      if [ "$previous_state" = active ]; then
        systemctl --user restart mihomo-proxy.service || printf '%s\n' '旧配置已恢复，但服务重启失败，请检查服务状态。' >&2
      else
        systemctl --user stop mihomo-proxy.service || printf '%s\n' '旧配置已恢复，但停止服务失败，请检查服务状态。' >&2
      fi
    else
      printf '%s\n' '自动恢复配置失败，请保留全部文件，使用下方手动恢复步骤。' >&2
    fi
  fi
  exit "$result"
}
trap restore_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -p -- private/config.yaml "$backup"
switch_started=1
systemctl --user stop mihomo-proxy.service
rm -- private/config.yaml
rm -f -- private/config.candidate.yaml
python3 configure.py "$HOME/mihomo-subscription.yaml" </dev/tty
./bin/mihomo -t -d "$HOME/mihomo/private" \
  -f "$HOME/mihomo/private/config.candidate.yaml" \
  > private/config-check.log 2>&1
mv -- private/config.candidate.yaml private/config.yaml
systemctl --user restart mihomo-proxy.service
systemctl --user is-active --quiet mihomo-proxy.service
curl --fail --silent --show-error \
  --connect-timeout 10 --max-time 30 --retry 2 --retry-connrefused \
  --noproxy '' --proxy http://127.0.0.1:17890 \
  "$f3_origin/api/v5/public/time" |
python3 -c 'import json,sys; r=json.load(sys.stdin); assert r.get("code")=="0" and r.get("data") and int(r["data"][0]["ts"])>0'
printf '%s\n' "新节点公开接口检查通过；原配置备份：$HOME/mihomo/$backup"
BASH
```

通过后，按第 1 节先在当前 SSH 窗口导出三个代理变量，再运行带完整配置和目录参数的 F3 `check`。然后查看 F3 `status` 的最近检查和行情推进时间是否持续更新。`status` 只读模拟账本，不会即时检测代理；其他共用该代理的量化应分别按其说明检查，不能用 F3 检查代替真钱账户的持仓、委托和止损核对。

自动恢复只是尝试，不保证旧节点恢复联网；突然断电、终端被强制终止或磁盘错误也可能让恢复未完成。需要手动恢复时，先在服务器 `~/mihomo` 查看备份名：

```bash
ls -lt "$HOME/mihomo/private"/config.backup-*.yaml
```

选定这次切换前的备份，把下面 `BACKUP_FILENAME` 替换成实际文件名，再执行：

```bash
cd "$HOME/mihomo"
cp -p -- "private/BACKUP_FILENAME" private/config.yaml &&
systemctl --user restart mihomo-proxy.service
```

最后重做第 1 节检查。不要直接删除唯一的可用配置，不要将配置备份提交 GitHub，也不要同时启动旧 `v53-proxy.service` 和新服务争用同一端口。

## 6. 日常管理与故障定位

以下按需要选择执行，不是需要从上到下一次全运行的脚本：

| 目的 | 服务器指令 |
| --- | --- |
| 查看进程是否运行 | `systemctl --user is-active mihomo-proxy.service` |
| 详细服务状态 | `systemctl --user status mihomo-proxy.service --no-pager` |
| 启动现有代理 | `systemctl --user start mihomo-proxy.service` |
| 重启，重新读取配置 | `systemctl --user restart mihomo-proxy.service` |
| 停止代理，所有使用它的程序都会受影响 | `systemctl --user stop mihomo-proxy.service` |
| 检查开机启动 | `systemctl --user is-enabled mihomo-proxy.service` |
| 查看最近代理输出 | `tail -n 80 "$HOME/mihomo/private/proxy.log"` |
| 连续观察日志 | `tail -F "$HOME/mihomo/private/proxy.log"` |
| 查看服务管理报错 | `journalctl --user -u mihomo-proxy.service -n 50 --no-pager` |

在 `tail -F` 中按 `Ctrl+C` 只退出日志查看，不会停止代理。日志可能含节点或连接信息，仅在本地阅读，不原样上传。服务正常但 API 超时、TLS EOF 时，检查节点质量；不要关闭 HTTPS 证书验证，也不要认为反复重新安装量化能修复线路。

订阅已过期、节点不可用、服务器未联网、Mihomo 停止、系统时间错误都可能导致联网检查失败。检查系统时间可用只读命令 `timedatectl status`。恢复后看本次 F3 `check` 结果、模拟账本的最新检查时间和行情推进时间，不用旧的成功输出代替当前检查。

## 7. 仓库中可以保留什么

本仓库只提供公开辅助脚本、服务模板和说明。不要提交以下内容：真实订阅及链接、`~/mihomo/private/`、`~/mihomo/bin/`、配置备份、代理日志、其它量化的 `credentials.env`、本项目 `.env` 和模拟账本。

`.gitignore` 只防止尚未跟踪的文件被普通提交；已经进入 Git 历史的凭据不会因此消失。如果秘密已经公开，需在相应服务商撤销/更换；交易所凭据如果涉及也须在交易所撤销/更换，不能只删除当前文件。

参考官方资料：[Mihomo 配置说明](https://wiki.metacubex.one/config/)、[Mihomo 官方发布](https://github.com/MetaCubeX/mihomo/releases)、[OKX API 文档](https://www.okx.com/docs-v5/zh/)。本页说明的是配置与检查流程，不代表已经在你的服务器运行这些命令。F3 只做本地公开行情模拟，不具备实际下单功能。
