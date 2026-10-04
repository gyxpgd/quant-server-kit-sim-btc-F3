"""Installer regression using only fake managers and temporary HOME/project trees."""
import configparser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNIT = 'okx-funding-f3.service'
FIELDS = ('LoadState', 'ActiveState', 'UnitFileState', 'TriggeredBy', 'DropInPaths', 'FragmentPath')


def properties(**changes):
    values = dict(LoadState='not-found', ActiveState='inactive', UnitFileState='',
                  TriggeredBy='', DropInPaths='', FragmentPath='')
    values.update(changes)
    return values


@unittest.skipIf(sys.version_info < (3, 10) or os.getuid() == 0,
                 'Installer requires ordinary-user Python 3.10+; run this suite as that user')
class UserServiceInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.project = self.base / 'F3 project'
        self.project.mkdir()
        shutil.copytree(ROOT / 'deploy', self.project / 'deploy')
        (self.project / 'paper.py').write_text('# never executed by installer\n')
        for variant in ('20u', '60u'):
            (self.project / ('config-F3-' + variant + '.json')).write_text('{}\n')
        self.bin = self.base / 'fake-bin'
        self.bin.mkdir()
        self.state_path = self.base / 'manager.json'
        self.log_path = self.base / 'calls.jsonl'
        self.state = {'system': {UNIT: properties()}, 'user': {UNIT: properties()}}
        self.state_path.write_text(json.dumps(self.state))
        (self.bin / 'python3').symlink_to(sys.executable)
        self.executable('id', '#!/bin/sh\nif [ "$1" = "-u" ]; then echo "${FAKE_UID:-1000}"; else echo test-user; fi\n')
        self.executable('uname', '#!/bin/sh\necho "${FAKE_OS:-Linux}"\n')
        self.executable('sudo', '#!/bin/sh\necho FORBIDDEN_SUDO >&2\nexit 99\n')
        self.executable('loginctl', '#!/bin/sh\necho "${FAKE_LINGER:-no}"\n')
        self.executable('systemctl', '#!' + sys.executable + '\n' + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['FAKE_CALL_LOG']).open('a') as stream:
    stream.write(json.dumps(args) + '\n')
state = json.loads(Path(os.environ['FAKE_MANAGER']).read_text())
scope = 'user' if '--user' in args else 'system'
if args == ['--user', 'show-environment']:
    sys.exit(state.get('manager_unavailable', 0))
if args == ['--user', 'daemon-reload']:
    if 'after_reload_user' in state:
        state['user'] = state['after_reload_user']
        Path(os.environ['FAKE_MANAGER']).write_text(json.dumps(state))
    sys.exit(state.get('reload_failure', 0))
if 'show' in args:
    if state.get('malformed_scope') == scope:
        print('unexpected response')
        sys.exit(1)
    row = state.get(scope, {}).get(args[-1])
    if row is None:
        sys.exit(1)
    for key, value in row.items():
        if value or '--all' in args:
            print(key + '=' + value)
    sys.exit(0)
print('Forbidden command', file=sys.stderr)
sys.exit(99)
''')
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                        FAKE_MANAGER=str(self.state_path), FAKE_CALL_LOG=str(self.log_path))
        self.target = self.home / '.config/systemd/user' / UNIT

    def tearDown(self):
        self.temp.cleanup()

    def executable(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_installer(self, *args, **environment):
        self.state_path.write_text(json.dumps(self.state))
        return subprocess.run(['bash', str(self.project / 'deploy/install-service.sh'), *args],
                              env=dict(self.env, **environment), capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()] if self.log_path.exists() else []

    def assert_refused(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(['--user', 'daemon-reload'], self.calls())
        self.assertFalse(self.target.exists())
        self.assertFalse((self.project / 'var').exists())

    def test_default_install_is_user_only_and_does_not_enable_or_start(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = self.target.read_text()
        self.assertIn('config-F3-20u.json', unit)
        self.assertIn('/var/20u', unit)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertIn(['--user', 'daemon-reload'], self.calls())
        self.assertTrue(all('show' in call or call in (['--user', 'show-environment'],
                                                       ['--user', 'daemon-reload']) for call in self.calls()))
        self.assertIn('Python：' + str(Path(sys.executable).resolve()), result.stdout)
        self.assertIn('配置文件：' + str(self.project.resolve() / 'config-F3-20u.json'), result.stdout)
        self.assertIn('没有 enable、启动', result.stdout)
        self.assertIn('systemctl --user enable --now ' + UNIT, result.stdout)
        self.assertFalse((self.home / 'etc').exists())

    def test_template_uses_user_target_proxy_and_env_without_user_directive(self):
        self.assertEqual(self.run_installer().returncode, 0)
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(self.target)
        self.assertEqual(unit['Unit']['Wants'], 'mihomo-proxy.service')
        self.assertEqual(unit['Unit']['After'], 'mihomo-proxy.service')
        self.assertEqual(unit['Install']['WantedBy'], 'default.target')
        self.assertNotIn('User', unit['Service'])
        self.assertEqual(unit['Service']['WorkingDirectory'], str(self.project.resolve()))
        self.assertEqual(unit['Service']['EnvironmentFile'], '-' + str(self.project.resolve() / '.env'))
        self.assertIn(' run --config ', unit['Service']['ExecStart'])
        self.assertNotIn('Requires', unit['Unit'])

    def test_explicit_sixty_variant_uses_separate_configuration_and_data(self):
        result = self.run_installer('60u')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('config-F3-60u.json', self.target.read_text())
        self.assertIn('/var/60u', self.target.read_text())
        self.assertNotIn('config-F3-20u.json', self.target.read_text())

    def test_root_is_rejected_before_manager_or_files(self):
        result = self.run_installer(FAKE_UID='0')
        self.assert_refused(result)
        self.assertEqual(self.calls(), [])
        self.assertIn('普通 Linux 用户', result.stderr)

    def test_non_linux_and_invalid_variant_are_rejected(self):
        self.assert_refused(self.run_installer(FAKE_OS='Darwin'))
        self.assert_refused(self.run_installer('30u'))
        self.assert_refused(self.run_installer('20u', '60u'))

    def test_unavailable_or_ambiguous_manager_state_is_rejected(self):
        for state in ({'manager_unavailable': 1}, {'malformed_scope': 'system'},
                      {'malformed_scope': 'user'}):
            with self.subTest(state=state):
                self.state.update(state)
                self.assert_refused(self.run_installer())
                for key in state:
                    del self.state[key]

    def test_system_active_or_enabled_service_is_rejected_without_mutation(self):
        for changes in (dict(ActiveState='active'), dict(ActiveState='activating'),
                        dict(ActiveState='deactivating'), dict(ActiveState='reloading'),
                        dict(UnitFileState='enabled'), dict(UnitFileState='enabled-runtime'),
                        dict(UnitFileState='static'), dict(LoadState='error')):
            with self.subTest(changes=changes):
                self.state['system'][UNIT] = properties(LoadState='loaded', UnitFileState='disabled')
                self.state['system'][UNIT].update(changes)
                self.assert_refused(self.run_installer())

    def test_inactive_disabled_system_unit_can_remain_for_rollback(self):
        for load, active, file_state in (('loaded', 'inactive', 'disabled'),
                                         ('loaded', 'failed', 'disabled'),
                                         ('masked', 'inactive', 'masked')):
            with self.subTest(file_state=file_state, active=active):
                self.state['system'][UNIT] = properties(LoadState=load, ActiveState=active,
                                                        UnitFileState=file_state,
                                                        FragmentPath='/etc/systemd/system/' + UNIT)
                result = self.run_installer()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(all('show' in c for c in self.calls() if '--system' in c))

    def test_active_or_enabled_automatic_trigger_is_rejected(self):
        trigger = 'okx-funding-f3.timer'
        self.state['system'][UNIT] = properties(LoadState='loaded', UnitFileState='disabled', TriggeredBy=trigger)
        for active, file_state in (('active', 'disabled'), ('inactive', 'enabled'),
                                  ('activating', 'static')):
            with self.subTest(active=active, file_state=file_state):
                self.state['system'][trigger] = properties(LoadState='loaded', ActiveState=active,
                                                           UnitFileState=file_state)
                self.assert_refused(self.run_installer())

    def test_disabled_inactive_trigger_allows_install(self):
        trigger = 'okx-funding-f3.timer'
        self.state['system'][UNIT] = properties(LoadState='loaded', UnitFileState='disabled', TriggeredBy=trigger)
        self.state['system'][trigger] = properties(LoadState='loaded', UnitFileState='disabled')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_user_active_or_transitional_service_cannot_be_overwritten(self):
        for active in ('active', 'activating', 'deactivating', 'reloading'):
            with self.subTest(active=active):
                self.state['user'][UNIT] = properties(LoadState='loaded', ActiveState=active)
                self.assert_refused(self.run_installer())

    def test_existing_different_unit_and_variant_switch_require_explicit_removal(self):
        self.target.parent.mkdir(parents=True)
        original = '# operator-maintained unit\n'
        self.target.write_text(original)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), original)
        self.assertNotIn(['--user', 'daemon-reload'], self.calls())
        self.target.unlink()
        self.assertEqual(self.run_installer('20u').returncode, 0)
        original = self.target.read_text()
        result = self.run_installer('60u')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), original)
        self.assertIn('备份并移走旧文件', result.stderr)

    def test_identical_inactive_install_is_idempotent(self):
        self.assertEqual(self.run_installer().returncode, 0)
        original = self.target.read_bytes()
        self.state['user'][UNIT] = properties(LoadState='loaded', UnitFileState='disabled',
                                              FragmentPath=str(self.target))
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), original)

    def test_manager_dropins_and_unloaded_local_dropin_directory_are_rejected(self):
        for scope in ('system', 'user'):
            with self.subTest(scope=scope):
                self.state[scope][UNIT]['DropInPaths'] = '/unused/override.conf'
                self.assert_refused(self.run_installer())
                self.state[scope][UNIT]['DropInPaths'] = ''
        (self.target.parent / (UNIT + '.d')).mkdir(parents=True)
        self.assert_refused(self.run_installer())

    def test_dropin_revealed_by_reload_is_rejected_without_starting(self):
        self.state['after_reload_user'] = {UNIT: properties(
            LoadState='loaded', UnitFileState='disabled', FragmentPath=str(self.target),
            DropInPaths='/etc/systemd/user/service.d/override.conf')}
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.target.exists())
        self.assertIn('drop-in', result.stderr)
        self.assertNotIn('用户服务已安装', result.stdout)
        self.assertTrue(all('show' in c or c == ['--user', 'show-environment']
                            or c == ['--user', 'daemon-reload'] for c in self.calls()))

    def test_symlink_target_and_other_loaded_fragment_are_rejected(self):
        self.target.parent.mkdir(parents=True)
        other = self.base / 'operator-unit'
        other.write_text('keep me\n')
        self.target.symlink_to(other)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(other.read_text(), 'keep me\n')
        self.target.unlink()
        self.state['user'][UNIT] = properties(LoadState='loaded', FragmentPath=str(other))
        self.assert_refused(self.run_installer())

    def test_absolute_path_spaces_percent_dollar_and_quotes_preserve_original_encoding(self):
        renamed = self.base / 'F3 space % quote" $cash'
        self.project.rename(renamed)
        self.project = renamed
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = self.target.read_text()
        project = str(self.project.resolve())
        self.assertIn('WorkingDirectory=' + project.replace('%', '%%') + '\n', unit)
        encoded_script = (project + '/paper.py').replace('%', '%%').replace('$', '$$').replace('"', '\\"')
        self.assertIn('"' + encoded_script + '" run --config ', unit)
        self.assertIn('EnvironmentFile=-' + project.replace('%', '%%') + '/.env', unit)

    def test_env_is_only_referenced_not_read_or_expanded_and_linger_only_reported(self):
        marker = 'proxy-local-test-value-not-a-real-secret'
        env = self.project / '.env'
        env.write_text('HTTPS_PROXY=' + marker + '\n')
        env.chmod(0o644)
        result = self.run_installer(FAKE_LINGER='no')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(env.read_text(), 'HTTPS_PROXY=' + marker + '\n')
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(marker, self.target.read_text() + result.stdout + result.stderr)
        self.assertIn('只读检查 linger', result.stdout)


if __name__ == '__main__':
    unittest.main()
