"""Offline packaging checks. No server, credential or network access."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('repo_guard', ROOT/'tools/check_repo.py')
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class RepositoryFilesTests(unittest.TestCase):
    def test_safe_public_files(self):
        for name in ('.env.example', 'config-F3-20u.json', 'config-F3-60u.json',
                     '.github/workflows/offline-tests.yml'):
            self.assertIsNone(GUARD.problem(name, (ROOT/name).read_bytes()))

    def test_private_paths_rejected(self):
        for name in ('.env', '.env.local', 'credentials.env', 'credentials.env.example',
                     'private/config.yaml', 'var/20u/paper.sqlite3', 'run.log',
                     'mihomo-subscription.yaml', 'another/.env.example', 'bin/mihomo'):
            self.assertIsNotNone(GUARD.problem(name, b'{}'), name)

    def test_proxy_example_is_exact(self):
        example=(ROOT/'.env.example').read_bytes()
        self.assertIsNotNone(GUARD.problem('.env.example', b'http_proxy=http://example.invalid\n'+example))
        self.assertIsNotNone(GUARD.problem('.env.example', example+b'OTHER=anything\n'))

    def test_json_key_fields_rejected(self):
        for data in (b'{"OKX_API_KEY":"fictional"}',
                     b'{"nested":{"OKX_SECRET_KEY":"fictional"}}',
                     b'{\n"OKX_PASSPHRASE":"fictional"\n}'):
            self.assertIsNotNone(GUARD.problem('settings.json',data))

    @unittest.skipUnless(shutil.which('git'), 'git is required for index checks')
    def test_git_index_and_ignore_rules(self):
        with tempfile.TemporaryDirectory(prefix='f3-repository-test-') as directory:
            root=Path(directory)
            (root/'tools').mkdir()
            shutil.copyfile(ROOT/'tools/check_repo.py',root/'tools/check_repo.py')
            shutil.copyfile(ROOT/'.gitignore',root/'.gitignore')
            def run(*args):
                return subprocess.run(args,cwd=root,capture_output=True,text=True)
            self.assertEqual(run('git','init','-q').returncode,0)
            for name in ('.env','var/20u/paper.sqlite3','private/config.yaml','sample-subscription.yaml'):
                self.assertEqual(run('git','check-ignore','-q',name).returncode,0,name)
            for name in ('.env.example','config-F3-20u.json','tools/check_repo.py'):
                self.assertEqual(run('git','check-ignore','-q',name).returncode,1,name)
            settings=root/'settings.json'
            settings.write_text('{"OKX_API_KEY":"fictional-index-only"}')
            self.assertEqual(run('git','add','.').returncode,0)
            settings.write_text('{}')
            rejected=run(sys.executable,'-B','tools/check_repo.py','--staged')
            self.assertEqual(rejected.returncode,1)
            self.assertNotIn('fictional-index-only',rejected.stdout+rejected.stderr)
            self.assertEqual(run('git','add','settings.json').returncode,0)
            self.assertEqual(run(sys.executable,'-B','tools/check_repo.py','--staged').returncode,0)
            (root/'link.json').symlink_to('settings.json')
            self.assertEqual(run('git','add','link.json').returncode,0)
            self.assertEqual(run(sys.executable,'-B','tools/check_repo.py','--staged').returncode,2)


if __name__ == '__main__':
    unittest.main()
