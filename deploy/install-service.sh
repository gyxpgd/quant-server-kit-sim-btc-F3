#!/usr/bin/env bash
# Ordinary-user installation only. Never start, enable, or alter a system unit.
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

[[ $(id -u) -ne 0 ]] || fail '请使用普通 Linux 用户运行此脚本，不要 sudo bash install-service.sh。'
[[ $(uname -s) == Linux ]] || fail '此安装脚本只用于 Linux；请先 SSH 登录 Linux 主机。'
[[ $# -le 1 ]] || fail '用法：bash deploy/install-service.sh [20u|60u]'
account_variant=${1:-20u}
case "$account_variant" in
    20u|60u) ;;
    *) fail '账户只能选择 20u 或 60u；两者都是虚拟余额。' ;;
esac
for executable in python3 systemctl; do
    command -v "$executable" >/dev/null 2>&1 || fail "缺少命令：$executable"
done

script_directory=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project_directory=$(CDPATH= cd -- "$script_directory/.." && pwd -P)
case "$script_directory$project_directory" in
    *$'\n'*|*$'\r'*) fail '程序目录不能包含换行或回车。' ;;
esac
[[ -f "$project_directory/paper.py" ]] || fail '安装包不完整：缺少 paper.py。'
[[ -f "$project_directory/config-F3-$account_variant.json" ]] || fail '安装包不完整：缺少账户配置。'
[[ -f "$script_directory/okx-funding-f3.service.in" ]] || fail '安装包不完整：缺少服务模板。'

umask 077
python3 - "$project_directory" "$account_variant" <<'PY'
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


def fail(message):
    raise SystemExit(message)


if sys.version_info < (3, 10):
    fail('需要 Python 3.10 或更高版本；不要使用旧系统 Python。')
if os.getuid() == 0:
    fail('拒绝为 root 账户安装模拟服务。')
project = Path(sys.argv[1]).resolve()
variant = sys.argv[2]
python = Path(sys.executable).resolve()
unit_name = 'okx-funding-f3.service'
unit_directory = Path.home() / '.config/systemd/user'
unit_target = unit_directory / unit_name


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        fail('无法读取服务管理器状态；尚未安装。请先检查 systemctl --user status 和 systemctl --system status。')


def service_properties(scope, name):
    fields = ('LoadState', 'ActiveState', 'UnitFileState', 'TriggeredBy', 'DropInPaths', 'FragmentPath')
    result = command(['systemctl', scope, 'show', '--all', '--no-pager',
                      '--property=' + ','.join(fields), '--', name])
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep or key not in fields or key in values:
            fail('服务状态响应无法确认；尚未安装。请先只读查看 systemctl ' + scope + ' status ' + name + '。')
        values[key] = value
    if (set(values) != set(fields) or not values.get('LoadState')
            or not values.get('ActiveState')
            or (result.returncode and values.get('LoadState') != 'not-found')):
        fail('无法确认服务状态；尚未安装。请先只读查看 systemctl ' + scope + ' status ' + name + '。')
    return values


def no_dropins(properties, scope):
    if properties['DropInPaths']:
        fail(scope + '服务存在 drop-in 配置；请先核对并迁移这些配置，安装脚本不会覆盖或忽略它们。')


def old_system_guard():
    old = service_properties('--system', unit_name)
    no_dropins(old, '系统级')
    if old['ActiveState'] not in ('inactive', 'failed'):
        fail('旧系统级 F3 服务正在运行或切换状态；请管理员先停止并禁用旧服务，再安装用户服务。')
    if old['LoadState'] == 'not-found':
        if old['UnitFileState'] not in ('', 'disabled', 'masked'):
            fail('旧系统级 F3 仍存在启用记录；请管理员先禁用旧服务并核对状态。')
    elif old['LoadState'] not in ('loaded', 'masked') or old['UnitFileState'] not in ('disabled', 'masked'):
        fail('旧系统级 F3 服务仍启用或状态不明确；请管理员先停止并禁用旧服务，不要同时启动两套。')
    for trigger in old['TriggeredBy'].split():
        if not re.fullmatch(r'[A-Za-z0-9_.@:\\-]+\.(?:timer|socket|path)', trigger) or trigger.startswith('-'):
            fail('旧系统级 F3 的触发来源无法确认；请管理员先核对自动触发配置。')
        trigger_state = service_properties('--system', trigger)
        no_dropins(trigger_state, '旧服务触发器')
        if (trigger_state['ActiveState'] not in ('inactive', 'failed')
                or trigger_state['UnitFileState'] not in ('disabled', 'masked')
                or trigger_state['LoadState'] not in ('loaded', 'masked')):
            fail('旧系统级 F3 有活动或已启用的自动触发器；请管理员先停止并禁用对应 timer/socket/path。')


def user_guard():
    current = service_properties('--user', unit_name)
    no_dropins(current, '用户级')
    if current['ActiveState'] not in ('inactive', 'failed'):
        fail('用户级 F3 服务正在运行或切换状态；先核对账本并 systemctl --user stop okx-funding-f3.service，再安装。')
    if current['LoadState'] not in ('loaded', 'not-found'):
        fail('用户级 F3 服务被屏蔽或加载状态异常；请先核对，不自动覆盖。')
    fragment = current['FragmentPath']
    if fragment and Path(fragment).resolve() != unit_target.resolve():
        fail('用户级 F3 正在使用其他位置的服务文件；请先核对并迁移，不自动覆盖。')
    if ((unit_directory / (unit_name + '.d')).exists()
            or (unit_directory / (unit_name + '.d')).is_symlink()):
        fail('用户级 F3 存在本地 drop-in 目录；请先核对并迁移，不自动覆盖。')


if command(['systemctl', '--user', 'show-environment']).returncode:
    fail('当前普通用户的 systemd 管理器不可用；请先登录正确用户并检查 systemctl --user status。')
old_system_guard()
user_guard()


def check_path(path):
    value = str(path)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail('路径不能包含换行、制表符或其他控制字符。')
    if '\\' in value or any(c in value for c in '*?[]') or value != value.strip():
        fail('路径不能包含反斜杠、通配符或首尾空白；目录中间的普通空格可以保留。')
    if not path.is_absolute():
        fail('服务配置必须使用绝对路径。')
    return value


def unit_path(path):
    # Preserve the original whole-path encoding for these two directives.
    return check_path(path).replace('%', '%%')


def exec_argument(path):
    # ExecStart has its own word parser and percent/environment expansion.
    value = check_path(path).replace('%', '%%').replace('$', '$$')
    return '"' + value.replace('"', '\\"') + '"'


check_path(project)
check_path(python)
data = project / 'var' / variant
env = project / '.env'
if env.is_symlink() or (env.exists() and not env.is_file()):
    fail('.env 必须是本目录中的普通文件，不能是符号链接。')
replacements = {
    '@WORKING_DIRECTORY@': unit_path(project),
    '@ENVIRONMENT_FILE@': unit_path(env),
    '@PYTHON@': exec_argument(python),
    '@PAPER_SCRIPT@': exec_argument(project / 'paper.py'),
    '@CONFIG@': exec_argument(project / ('config-F3-' + variant + '.json')),
    '@DATA_DIR@': exec_argument(data),
}
template = (project / 'deploy' / 'okx-funding-f3.service.in').read_text(encoding='utf-8')
if set(re.findall(r'@[A-Z_]+@', template)) != set(replacements):
    fail('服务模板占位符与安装脚本不匹配。')
unit = re.sub(r'@[A-Z_]+@', lambda match: replacements[match.group()], template)
if unit_target.is_symlink():
    fail('目标用户服务文件是符号链接；请先核对，不自动覆盖。')
if unit_target.exists():
    if not unit_target.is_file() or unit_target.read_text(encoding='utf-8') != unit:
        fail('已存在不同的用户级 F3 服务文件；先停止服务、备份并移走旧文件、daemon-reload，再显式安装所选账户。')

# Only after all guards pass may local installation files be created.
unit_directory.mkdir(parents=True, exist_ok=True)
if not unit_target.exists():
    descriptor = os.open(unit_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(unit)
    unit_target.chmod(0o644)
data.mkdir(parents=True, exist_ok=True, mode=0o700)
if env.exists():
    # Never read, print, or interpolate the proxy configuration.
    env.chmod(0o600)
if command(['systemctl', '--user', 'daemon-reload']).returncode:
    fail('用户服务文件已保存，但 daemon-reload 失败；尚未启动，请先核对当前用户的 systemd。')
# Reload can reveal inherited/global drop-ins not visible for a missing unit.
user_guard()

print('运行用户：' + pwd.getpwuid(os.getuid()).pw_name)
print('程序目录：' + str(project))
print('Python：' + str(python))
print('配置文件：' + str(project / ('config-F3-' + variant + '.json')))
print('账户选择：' + variant + '（虚拟余额）')
print('数据目录：' + str(data))
print('用户服务已安装；本次没有 enable、启动或修改系统级服务。')
print('确认配置和账本后，由你执行：')
print('  systemctl --user enable --now okx-funding-f3.service')
print('  systemctl --user status okx-funding-f3.service --no-pager')
try:
    linger = subprocess.run(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value'],
                            capture_output=True, text=True, timeout=5)
    linger_value = linger.stdout.strip() if linger.returncode == 0 else 'unknown'
except (OSError, subprocess.TimeoutExpired):
    linger_value = 'unknown'
if linger_value == 'yes':
    print('Linger=yes：用户服务可在退出 SSH 后保留，并可随系统启动。')
else:
    print('Linger 尚未确认启用；若需退出 SSH 后和开机持续运行，请管理员为当前用户一次性启用 linger。')
    print('本脚本只读检查 linger，不修改登录或系统设置；日常管理使用 systemctl --user。')
PY
