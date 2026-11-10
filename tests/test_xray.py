import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import code_xray as x


class XrayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_python_ast_and_no_execution(self):
        self.write('main.py', 'import os\nfrom .data import read\ndef main():\n    return read()\nif __name__ == "__main__":\n    raise RuntimeError("never execute")\n')
        r = x.scan(self.root)
        self.assertEqual(r['symbols'][0]['name'], 'main')
        self.assertEqual(r['imports'][1]['module'], '.data')
        self.assertEqual(r['entry_points'][0]['line'], 5)
        self.assertIn('read', [c['target'] for c in r['calls']])

    def test_manifest_evidence(self):
        self.write('package.json', json.dumps({'scripts': {'dev': 'vite'}, 'dependencies': {'react': '^19'}, 'bin': 'cli.js'}))
        self.write('pyproject.toml', '[project]\ndependencies=["pandas>=2"]\n[project.scripts]\nxray="pkg.cli:main"\n')
        r = x.scan(self.root)
        self.assertEqual(len(r['dependencies']), 2)
        self.assertEqual(r['commands'][0]['command'], 'vite')
        self.assertTrue(any('pkg.cli:main' in e['value'] for e in r['entry_points']))

    def test_exclusions_and_links(self):
        self.write('.env.local', 'OPENAI_API_KEY=secret')
        self.write('node_modules/x.js', 'fetch("x")')
        self.write('data/private.py', 'secret=1')
        self.write('real.py', 'x=1')
        (self.root / 'alias.py').symlink_to(self.root / 'real.py')
        r = x.scan(self.root, exclude=['data'])
        self.assertEqual([f['path'] for f in r['files']], ['real.py'])
        self.assertFalse(r['signals'])

    def test_bounds_and_bad_source(self):
        self.write('a.py', 'def broken(')
        self.write('b.py', 'x' * 300)
        r = x.scan(self.root, max_bytes=100)
        self.assertTrue(any('parsing failed' in w for w in r['warnings']))
        self.assertEqual(r['summary']['skipped_content']['oversize'], 1)
        r = x.scan(self.root, max_files=1)
        self.assertEqual(len(r['files']), 1)
        self.assertTrue(any('File limit' in w for w in r['warnings']))

    def test_js_clues_and_signals(self):
        self.write('app/transport.ts', 'import {x} from "openai";\nfetch("https://api.openai.com/v1/agents/sessions");\n')
        r = x.scan(self.root)
        self.assertEqual(r['imports'][0]['module'], 'openai')
        self.assertEqual(r['signals'][0]['line'], 2)
        self.assertNotIn('https://', json.dumps(r['signals']))

    def test_json_output_and_refuse_overwrite(self):
        self.write('main.py', 'pass')
        output = self.root / 'report.json'
        self.assertEqual(x.main(['scan', str(self.root), '--json', '-o', str(output)]), 0)
        self.assertEqual(json.loads(output.read_text())['schema_version'], 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(x.main(['scan', str(self.root), '-o', str(output)]), 1)

    def test_remote_credentials_rejected_before_git(self):
        with self.assertRaises(ValueError):
            x.remote_scan('https://token@example.com/repo.git', None)

    @unittest.skipUnless(x.shutil.which('git'), 'git unavailable')
    def test_remote_snapshot_with_local_git_fixture(self):
        self.write('main.py', 'def hello():\n    return 42\n')
        self.write('.env', 'secret=hidden')
        (self.root / 'link.py').symlink_to('main.py')
        x.run(['git', 'init', str(self.root)])
        x.run(['git', '-C', str(self.root), 'add', '.'])
        x.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture'])
        original = x.run
        def local_clone(argv, **kwargs):
            if 'clone' in argv:
                argv = ['protocol.file.allow=always' if a == 'protocol.file.allow=never' else a for a in argv]
                argv[-2] = str(self.root)
            return original(argv, **kwargs)
        with patch.object(x, 'run', side_effect=local_clone):
            r = x.remote_scan('https://example.invalid/repo.git', SimpleNamespace(exclude=[], max_files=100, max_bytes=262144))
        self.assertEqual(len(r['commit']), 40)
        self.assertEqual([f['path'] for f in r['files']], ['main.py'])
        self.assertEqual(r['symbols'][0]['name'], 'hello')

    def test_rpm_queries_no_install(self):
        responses = ['demo\n1-1\nx86_64\nExample\n', 'python3\n', 'demo = 1\n', '/usr/bin/demo\n', 'postinstall scriptlet: echo example\n']
        with patch.object(x.shutil, 'which', return_value='/usr/bin/rpm'), patch.object(x, 'run', side_effect=responses) as run:
            r = x.rpm_report('demo', installed=True)
            self.assertEqual(r['metadata']['name'], 'demo')
            for call in run.call_args_list:
                self.assertEqual(call.args[0][:2], ['rpm', '-q'])
                self.assertNotIn('-i', call.args[0])

    def test_terminal_controls_removed(self):
        self.assertNotIn('\x1b', x.safe_text('bad\x1b[2J'))


if __name__ == '__main__':
    unittest.main()
