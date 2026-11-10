#!/usr/bin/env python3
"""Read-only source and RPM orientation. Python 3.11+, standard library only."""
import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
from urllib.parse import urlsplit

VERSION = '0.1.0'
SKIP = {'.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__',
        'dist', 'build', '.next', '.wrangler', '.tox', '.mypy_cache', '.pytest_cache',
        'vendor', 'site-packages', 'target'}
LOCKS = {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'Cargo.lock', 'uv.lock', 'poetry.lock'}
EXTENSIONS = {'.py': 'Python', '.ts': 'TypeScript', '.tsx': 'TypeScript/JSX',
              '.js': 'JavaScript', '.jsx': 'JavaScript/JSX', '.mjs': 'JavaScript',
              '.go': 'Go', '.rs': 'Rust', '.c': 'C', '.h': 'C header', '.cpp': 'C++',
              '.sh': 'Shell', '.css': 'CSS', '.html': 'HTML', '.rst': 'reStructuredText',
              '.md': 'Markdown', '.sql': 'SQL', '.json': 'JSON', '.toml': 'TOML',
              '.yaml': 'YAML', '.yml': 'YAML'}

def run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, timeout=90, **kwargs)
    if result.returncode:
        # Provider errors may echo credential-bearing URLs: don't echo stderr.
        raise ValueError(f'{argv[0]} failed (exit {result.returncode})')
    return result.stdout.decode('utf-8', errors='replace')

def secret_file(name):
    n = name.lower()
    return (n.startswith('.env') or n in {'credentials', 'credentials.json', 'id_rsa', 'id_ed25519', '.netrc', '.npmrc', '.pypirc'}
            or n.endswith(('.pem', '.key', '.p12', '.pfx')))

def role(path):
    """Name-based clues only: these are explicitly not semantic conclusions."""
    p = Path(path); n = p.name.lower(); parts = set(p.parts[:-1])
    if n.startswith(('readme', 'license', 'copying')) or 'docs' in parts: return 'Documentation'
    if n in {'package.json', 'pyproject.toml', 'cargo.toml', 'go.mod', 'setup.cfg', 'setup.py'} or n.startswith('requirements'): return 'Manifest'
    if n in {'makefile', 'dockerfile', 'compose.yaml', 'compose.yml'} or n.endswith('.spec'): return 'Build / packaging'
    if 'tests' in parts or n.startswith('test_') or re.search(r'\.(test|spec)\.', n): return 'Tests'
    if n in {'main.py', '__main__.py', 'main.go', 'main.rs', 'cli.py'}: return 'Entry candidate'
    if parts & {'api', 'routes', 'controllers'} or n in {'route.ts', 'route.js'}: return 'Request handling'
    if parts & {'clients', 'adapters', 'integrations'} or 'transport' in n: return 'External connection'
    if parts & {'components', 'templates', 'views'} or p.suffix in {'.tsx', '.jsx', '.css', '.html'}: return 'Interface'
    if parts & {'db', 'migrations', 'drizzle'} or p.suffix == '.sql': return 'Storage'
    if parts & {'services', 'pipelines'}: return 'Processing candidate'
    return 'Source / configuration'

def evidence_line(text, key):
    return next((i for i, line in enumerate(text.splitlines(), 1) if key in line), 1)

def inspect_python(text, path, report):
    try: tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        report['warnings'].append(f'{path}: Python parsing failed ({type(exc).__name__})'); return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            report['symbols'].append({'file': path, 'line': node.lineno, 'name': node.name,
                                      'kind': 'class' if isinstance(node, ast.ClassDef) else 'function', 'evidence': 'Python AST'})
        elif isinstance(node, ast.Import):
            for a in node.names: report['imports'].append({'file': path, 'line': node.lineno, 'module': a.name, 'evidence': 'Python AST'})
        elif isinstance(node, ast.ImportFrom):
            report['imports'].append({'file': path, 'line': node.lineno, 'module': '.' * node.level + (node.module or ''), 'names': [a.name for a in node.names], 'evidence': 'Python AST'})
        elif isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            t = node.test
            if (isinstance(t.left, ast.Name) and t.left.id == '__name__' and len(t.ops) == 1
                and isinstance(t.ops[0], ast.Eq) and len(t.comparators) == 1
                and isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value == '__main__'):
                report['entry_points'].append({'file': path, 'line': node.lineno, 'value': '__main__ guard', 'evidence': 'Python AST'})
        elif isinstance(node, ast.Call):
            # Syntactic call sites, deliberately not a resolved call graph.
            target = node.func
            if isinstance(target, (ast.Name, ast.Attribute)):
                report['calls'].append({'file': path, 'line': node.lineno, 'target': ast.unparse(target), 'evidence': 'Python AST; target not resolved'})

def inspect_manifest(text, path, report):
    name = Path(path).name
    def dep(ecosystem, package, version, group):
        report['dependencies'].append({'file': path, 'line': evidence_line(text, package), 'ecosystem': ecosystem,
            'name': package, 'requirement': str(version), 'group': group, 'evidence': 'Manifest declaration'})
    def entry(value, key):
        report['entry_points'].append({'file': path, 'line': evidence_line(text, key), 'value': value, 'evidence': 'Manifest declaration'})
    try:
        if name == 'package.json':
            obj = json.loads(text)
            for group in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
                for k, v in obj.get(group, {}).items(): dep('npm', k, v, group)
            for k, v in obj.get('scripts', {}).items():
                report['commands'].append({'file': path, 'line': evidence_line(text, k), 'name': k, 'command': v, 'evidence': 'Manifest declaration; not executed'})
            for key in ('main', 'module', 'bin'):
                if key in obj: entry(f'{key}: {obj[key]}', key)
        elif name == 'pyproject.toml':
            obj = tomllib.loads(text); project = obj.get('project', {})
            for raw in project.get('dependencies', []): dep('Python', raw, '', 'runtime')
            for group, items in project.get('optional-dependencies', {}).items():
                for raw in items: dep('Python', raw, '', group)
            for k, v in project.get('scripts', {}).items(): entry(f'{k} → {v}', k)
            poetry = obj.get('tool', {}).get('poetry', {})
            for k, v in poetry.get('dependencies', {}).items(): dep('Poetry', k, v, 'runtime')
            for group, config in poetry.get('group', {}).items():
                for k, v in config.get('dependencies', {}).items(): dep('Poetry', k, v, group)
        elif name == 'Cargo.toml':
            obj = tomllib.loads(text)
            for group in ('dependencies', 'dev-dependencies', 'build-dependencies'):
                for k, v in obj.get(group, {}).items(): dep('Cargo', k, v, group)
        elif name.startswith('requirements') and name.endswith('.txt'):
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith(('#', '-')): dep('Python', line, '', 'requirements')
        elif name == 'go.mod':
            inside = False
            for line in text.splitlines():
                line = line.split('//')[0].strip()
                if line == 'require (': inside = True; continue
                if line == ')': inside = False; continue
                match = re.match(r'(?:require\s+)?([^\s]+)\s+(v\S+)$', line)
                if match and (inside or line.startswith('require ')): dep('Go', *match.groups(), 'require')
    except (ValueError, TypeError, AttributeError) as exc:
        report['warnings'].append(f'{path}: manifest parsing failed ({type(exc).__name__})')

def scan(root, max_files=5000, max_bytes=262144, exclude=()):
    root = Path(root).expanduser().resolve()
    if not root.exists(): raise ValueError('Path does not exist')
    report = {'schema_version': 1, 'kind': 'source', 'source': str(root), 'files': [],
              'entry_points': [], 'commands': [], 'dependencies': [], 'imports': [],
              'symbols': [], 'calls': [], 'signals': [], 'warnings': [],
              'limitations': ['Roles are filename/path heuristics.', 'Non-Python imports and provider signals are text matches, not semantic proof.',
                  'Python calls are syntactic references, not a resolved execution graph.',
                  'No repository code, build scripts, or package installation is executed.',
                  'Common generated directories, lockfiles, environment/credential files, and symlinks are skipped. Other source may still contain sensitive text.',
                  '.gitignore is not interpreted; use --exclude for extra directory names.',
                  'Manifest evidence line numbers use the first text match; AST lines are exact.']}
    skipped = Counter(); languages = Counter(); total_read = 0
    def walk():
        if root.is_file(): yield root; return
        for parent, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and d not in exclude and not Path(parent, d).is_symlink())
            for name in sorted(names): yield Path(parent, name)
    for p in walk():
        if len(report['files']) >= max_files:
            report['warnings'].append(f'File limit reached ({max_files}); inventory is incomplete'); break
        if p.is_symlink() or secret_file(p.name) or p.name in LOCKS:
            skipped['excluded'] += 1; continue
        rel = p.name if root.is_file() else p.relative_to(root).as_posix()
        try:
            size = p.stat().st_size
            if not p.is_file(): continue
            language = EXTENSIONS.get(p.suffix.lower(), 'Other')
            row = {'path': rel, 'bytes': size, 'language': language, 'role': role(rel), 'evidence': 'Filename/path heuristic', 'content_scanned': False}
            report['files'].append(row); languages[language] += 1
            if size > max_bytes: skipped['oversize'] += 1; continue
            if total_read + size > 32 * 1024 * 1024: skipped['total_byte_limit'] += 1; continue
            with p.open('rb') as handle: raw = handle.read(max_bytes + 1)
            if len(raw) > max_bytes: skipped['oversize'] += 1; continue
            if b'\0' in raw: skipped['binary'] += 1; continue
            try: text = raw.decode('utf-8')
            except UnicodeDecodeError: skipped['non_utf8'] += 1; continue
            total_read += len(raw); row['content_scanned'] = True
            if p.suffix == '.py': inspect_python(text, rel, report)
            inspect_manifest(text, rel, report)
            if p.suffix.lower() in {'.js', '.jsx', '.ts', '.tsx', '.mjs'}:
                for match in re.finditer(r'''(?:\bfrom\s*|\bimport\s*|\brequire\s*\(\s*)["']([^"']+)["']''', text):
                    report['imports'].append({'file': rel, 'line': text.count('\n', 0, match.start()) + 1, 'module': match[1], 'evidence': 'Text match; may include comments'})
            for number, line in enumerate(text.splitlines(), 1):
                for label, pattern in [('OpenAI reference', r'\bOPENAI_API_KEY\b|api\.openai\.com|/v1/agents/sessions'),
                                       ('Ollama reference', r'\bOLLAMA_HOST\b|\bollama\b'),
                                       ('HTTP request reference', r'\bfetch\s*\(|\brequests\.(get|post)\s*\(')]:
                    if re.search(pattern, line):
                        report['signals'].append({'file': rel, 'line': number, 'name': label, 'evidence': 'Text match; source line omitted'})
        except OSError as exc:
            report['warnings'].append(f'{rel}: unreadable ({type(exc).__name__})')
    report['summary'] = {'files': len(report['files']), 'languages': dict(languages.most_common()),
                         'content_bytes_read': total_read, 'skipped_content': dict(skipped)}
    if skipped: report['warnings'].append('Some content was skipped; see summary.skipped_content')
    return report

def remote_scan(url, options):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTPS repository clone URL without credentials, query, or fragment')
    if not shutil.which('git'): raise ValueError('Remote inspection requires git')
    with tempfile.TemporaryDirectory(prefix='code-xray-') as tmp:
        repo = str(Path(tmp, 'repo')); source = Path(tmp, 'source'); source.mkdir()
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1')
        run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
             '-c', 'protocol.file.allow=never', 'clone', '--depth=1', '--no-checkout', '--', url, repo], env=env)
        commit = run(['git', '-C', repo, 'rev-parse', 'HEAD'], env=env).strip()
        # Stream archive members without extraction of links, devices, or traversal paths.
        proc = subprocess.Popen(['git', '-C', repo, '-c', 'core.hooksPath=/dev/null', 'archive', '--format=tar', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        total = count = 0; limited = False
        try:
            with tarfile.open(fileobj=proc.stdout, mode='r|') as archive:
                for member in archive:
                    parts = Path(member.name).parts
                    if (not member.isfile() or Path(member.name).is_absolute() or '..' in parts
                        or set(parts[:-1]) & (SKIP | set(options.exclude)) or secret_file(parts[-1]) or parts[-1] in LOCKS): continue
                    if member.size > options.max_bytes: continue
                    if count >= options.max_files or total + member.size > 32 * 1024 * 1024:
                        limited = True; break
                    target = source / member.name; target.parent.mkdir(parents=True, exist_ok=True)
                    stream = archive.extractfile(member)
                    if stream:
                        target.write_bytes(stream.read()); total += member.size; count += 1
            if not limited and proc.wait(timeout=30): raise ValueError('git archive failed')
        finally:
            if proc.poll() is None: proc.kill()
            proc.wait(); proc.stdout.close()
        result = scan(source, options.max_files, options.max_bytes, options.exclude)
        result['source'] = url; result['commit'] = commit
        result['warnings'].append('Remote scan: default branch only; links, submodules, LFS payloads, oversized files and generated directories are omitted before scanning. Inventory covers materialized files only.')
        if limited: result['warnings'].append('Remote materialization limit reached; source is incomplete')
        return result

def rpm_report(target, installed=False):
    if not shutil.which('rpm'): raise ValueError('RPM inspection requires the rpm command')
    if not installed:
        p = Path(target).expanduser().resolve()
        if not p.is_file(): raise ValueError('RPM file does not exist; use --installed for an installed package name')
        target = str(p)
    elif target.startswith('-'): raise ValueError('Invalid package name')
    base = ['rpm', '-q' if installed else '-qp']
    metadata = run(base + ['--qf', '%{NAME}\n%{VERSION}-%{RELEASE}\n%{ARCH}\n%{SUMMARY}\n', target]).splitlines()
    return {'schema_version': 1, 'kind': 'rpm', 'source': target,
        'metadata': dict(zip(('name', 'version_release', 'arch', 'summary'), metadata)),
        'requires': run(base + ['--requires', target]).splitlines(),
        'provides': run(base + ['--provides', target]).splitlines(),
        'files': run(base + ['--list', target]).splitlines(),
        'scriptlets': run(base + ['--scripts', target]),
        'limitations': ['RPM metadata and scriptlet text only; nothing is installed or executed.',
                        'An RPM payload is not necessarily source code. This report does not verify package trust or provenance.']}

def safe_text(value):
    # Keep terminal control characters from repository-controlled strings inert.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))

def table(headers, rows):
    widths = [24 if i == 0 else 58 for i in range(len(headers))]
    output = ['  '.join(h.ljust(w) for h, w in zip(headers, widths)), '  '.join('─' * w for w in widths)]
    for row in rows:
        cells = [textwrap.wrap(safe_text(v), width=w, break_long_words=True) or [''] for v, w in zip(row, widths)]
        for i in range(max(map(len, cells))): output.append('  '.join((c[i] if i < len(c) else '').ljust(w) for c, w in zip(cells, widths)).rstrip())
    return '\n'.join(output)

def render(report, section, limit):
    lines = [f'Code X-Ray {VERSION} · {safe_text(report["source"])}']
    if report['kind'] == 'rpm':
        lines.append(table(['Field', 'Value'], report['metadata'].items()))
        groups = [('Requires', report['requires']), ('Provides', report['provides']), ('Files', report['files'])]
        for title, items in groups:
            lines.append(f'\n{title} ({len(items)})'); lines.extend(safe_text(x) for x in items[:limit])
            if len(items) > limit: lines.append(f'… {len(items)-limit} more; use --json for all')
        lines.append('\nScriptlets (text only)\n' + safe_text(report['scriptlets']))
    else:
        s = report['summary']; lines.append(f'{s["files"]} files · ' + ', '.join(f'{k}: {v}' for k, v in s['languages'].items()))
        groups = {'files': ('File roles · inferred', [(x['role'], x['path']) for x in report['files']]),
                  'entries': ('Entry points · evidence', [(x['value'], f'{x["file"]}:{x["line"]}') for x in report['entry_points']]),
                  'commands': ('Declared commands · never executed', [(x['name'], f'{x["command"]} [{x["file"]}:{x["line"]}]') for x in report['commands']]),
                  'deps': ('Dependencies · declared', [(x['name'], f'{x["requirement"]} ({x["group"]}) [{x["file"]}:{x["line"]}]') for x in report['dependencies']]),
                  'imports': ('Imports', [(x['module'], f'{x["file"]}:{x["line"]}') for x in report['imports']]),
                  'symbols': ('Python symbols · AST', [(x['name'], f'{x["kind"]} [{x["file"]}:{x["line"]}]') for x in report['symbols']]),
                  'calls': ('Python calls · unresolved targets', [(x['target'], f'{x["file"]}:{x["line"]}') for x in report['calls']]),
                  'signals': ('Integration clues · text matches', [(x['name'], f'{x["file"]}:{x["line"]}') for x in report['signals']])}
        for key, (title, rows) in groups.items():
            if section not in ('overview', key): continue
            lines.append(f'\n{title} ({len(rows)})\n' + table(['What', 'Where / details'], rows[:limit]))
            if len(rows) > limit: lines.append(f'… {len(rows)-limit} more; use --limit or --json')
    for item in report.get('warnings', []) + report['limitations']: lines.append('Note: ' + safe_text(item))
    return '\n'.join(lines)

def positive(value):
    value = int(value)
    if value < 1: raise argparse.ArgumentTypeError('must be positive')
    return value

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='action', required=True)
    source = sub.add_parser('scan', help='Inspect a local path or HTTPS GitHub/GitLab clone URL')
    source.add_argument('target', nargs='?', default='.')
    source.add_argument('--section', choices=['overview', 'files', 'entries', 'commands', 'deps', 'imports', 'symbols', 'calls', 'signals'], default='overview')
    source.add_argument('--max-files', type=positive, default=5000)
    source.add_argument('--max-bytes', type=positive, default=262144, help='maximum bytes read per file')
    source.add_argument('--exclude', action='append', default=[], help='extra directory basename to omit (repeatable)')
    package = sub.add_parser('rpm', help='Query an RPM file or installed RPM; never install it')
    package.add_argument('target')
    package.add_argument('--installed', action='store_true')
    for p in (source, package):
        p.add_argument('--json', action='store_true', help='complete machine-readable report; --section affects tables only')
        p.add_argument('--limit', type=positive, default=20, help='rows per table section')
        p.add_argument('-o', '--output', type=Path, help='write report, refuse an existing output file')
    args = parser.parse_args(argv)
    try:
        if args.action == 'rpm': result = rpm_report(args.target, args.installed)
        elif urlsplit(args.target).scheme in ('http', 'https', 'ssh', 'git'): result = remote_scan(args.target, args)
        else: result = scan(args.target, args.max_files, args.max_bytes, args.exclude)
        output = json.dumps(result, indent=2, ensure_ascii=True) if args.json else render(result, getattr(args, 'section', 'overview'), args.limit)
        if args.output:
            with args.output.expanduser().open('x', encoding='utf-8') as handle: handle.write(output + '\n')
        else: print(output)
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        print(f'code-xray: {safe_text(exc)}', file=sys.stderr); return 1

if __name__ == '__main__':
    raise SystemExit(main())
