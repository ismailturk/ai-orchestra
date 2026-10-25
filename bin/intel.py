#!/usr/bin/env python3
"""Project memory (SQLite FTS5), skill suggestions and the shadow-mode context engine.

Read-only on project source: writes only .ai/memory.sqlite and .ai/context-shadow/. Never sends data anywhere.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team

# Never indexed, whatever .gitignore says: secrets, keys, credentials and agent session state.
SECRET_RE = re.compile(r'(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx|jks|keystore|mobileprovision|cer|crt|der|kdbx)'
                       r'|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|\.netrc|\.npmrc|\.pypirc|credentials(\..*)?|secrets?(\..*)?'
                       r'|.*service[-_]?account.*\.json|google-services\.json|GoogleService-Info\.plist'
                       r'|appsettings\.(Production|Staging)\.json|.*\.tfstate)$', re.I)
SKIP_DIRS = ('.git/', '.ai/runner-logs/', '.ai/messages/', '.ai/context-shadow/', '.claude/', '.codex/',
             '.cmux/', 'build/', '.dart_tool/', 'node_modules/', 'Pods/', '.gradle/', '~/')
MAX_BYTES = 400_000
# Redaction for anything shown or indexed: token-like literals and private key blocks.
REDACT_RE = re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----'
                       r'|\b(sk-(ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}'
                       r'|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})'
                       r'|((password|passwd|parola|secret|api[_-]?key|token|connectionstring)\s*["\']?\s*[:=]\s*)["\']?[^\s"\',;]{4,})',
                       re.I | re.S)


def redact(text):
    return REDACT_RE.sub(lambda m: (m.group(4) or '') + '«gizlendi»', text)


def secret_path(rel):
    return bool(SECRET_RE.search(rel))


def db(root):
    c = sqlite3.connect(root / '.ai' / 'memory.sqlite')
    c.executescript('''
    create table if not exists files(path text primary key, sha text, size int, lang text, git_rev text, indexed text);
    create virtual table if not exists fts using fts5(path, body, tokenize='unicode61');
    create table if not exists imports(src text, dst text);
    create table if not exists facts(id integer primary key, kind text not null
        check(kind in ('verified','user_decision','inference')), text text not null, source_path text,
        source_sha text, status text not null default 'current', auto int default 0, created text, updated text);
    create table if not exists meta(k text primary key, v text);''')
    return c


LANGS = {'.dart': 'dart', '.cs': 'csharp', '.py': 'python', '.ts': 'ts', '.tsx': 'ts', '.js': 'js', '.jsx': 'js',
         '.swift': 'swift', '.kt': 'kotlin', '.java': 'java', '.go': 'go', '.rs': 'rust', '.md': 'markdown',
         '.yaml': 'yaml', '.yml': 'yaml', '.json': 'json', '.sql': 'sql', '.html': 'html', '.css': 'css'}
IMPORT_RES = {'dart': re.compile(r"^\s*(?:import|export)\s+'([^']+)'", re.M),
              'python': re.compile(r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))', re.M),
              'ts': re.compile(r'''from\s+['"]([^'"]+)['"]'''), 'js': re.compile(r'''(?:from\s+|require\()['"]([^'"]+)['"]'''),
              'csharp': re.compile(r'^\s*using\s+([\w.]+)\s*;', re.M)}


def indexable(rel):
    return not rel.startswith(SKIP_DIRS) and '/node_modules/' not in rel and not secret_path(rel)


def index(root):
    """Incremental: only files whose sha changed are re-read; facts sourced from a changed file go stale."""
    root = Path(root)
    rev = team.git(root, 'rev-parse', 'HEAD').stdout.strip() or None
    c = db(root)
    known = dict(c.execute('select path, sha from files'))
    seen, changed, skipped = set(), 0, 0
    for rel in team.project_files(root):
        if not indexable(rel):
            skipped += 1
            continue
        f = root / rel
        if not f.is_file() or f.is_symlink() or f.stat().st_size > MAX_BYTES:
            continue
        data = f.read_bytes()
        if b'\0' in data[:8192]:
            continue
        seen.add(rel)
        sha = hashlib.sha256(data).hexdigest()
        if known.get(rel) == sha:
            continue
        changed += 1
        lang = LANGS.get(f.suffix.lower(), '')
        body = redact(data.decode(errors='replace'))
        c.execute('delete from fts where path=?', (rel,)); c.execute('delete from imports where src=?', (rel,))
        c.execute('insert into fts(path, body) values(?,?)', (rel, body))
        c.execute('insert or replace into files values(?,?,?,?,?,?)', (rel, sha, len(data), lang, rev, team.now()))
        for m in (IMPORT_RES[lang].finditer(body) if lang in IMPORT_RES else ()):
            c.execute('insert into imports values(?,?)', (rel, next((g for g in m.groups() if g), '')))
        c.execute("update facts set status='stale', updated=? where source_path=? and source_sha!=? and status='current'",
                  (team.now(), rel, sha))
    for rel in set(known) - seen:  # deleted, ignored or now secret: drop the content
        c.execute('delete from files where path=?', (rel,)); c.execute('delete from fts where path=?', (rel,))
        c.execute('delete from imports where src=?', (rel,))
        c.execute("update facts set status='stale', updated=? where source_path=? and status='current'", (team.now(), rel))
    refresh_verified(root, c)
    c.execute("insert or replace into meta values('indexed', ?)", (team.now(),))
    c.execute("insert or replace into meta values('git_rev', ?)", (rev or '',))
    c.commit(); c.close()
    return {'files': len(seen), 'changed': changed, 'skipped_secret_or_tool': skipped, 'git_rev': rev}


TECH = [('pubspec.yaml', 'Flutter/Dart'), ('.csproj', '.NET'), ('package.json', 'Node/JS'), ('pyproject.toml', 'Python'),
        ('requirements.txt', 'Python'), ('Package.swift', 'Swift'), ('go.mod', 'Go'), ('Cargo.toml', 'Rust'),
        ('build.gradle', 'Android/Gradle'), ('Podfile', 'iOS/CocoaPods')]


def refresh_verified(root, c):
    """Source-derived facts (auto=1) are rebuilt from the index on every run; they cite their source file."""
    c.execute("delete from facts where auto=1")
    files = dict(c.execute('select path, sha from files'))
    add = lambda text, src: c.execute(
        "insert into facts(kind,text,source_path,source_sha,status,auto,created,updated) values('verified',?,?,?,'current',1,?,?)",
        (text, src, files.get(src), team.now(), team.now()))
    for marker, tech in TECH:
        hit = sorted(p for p in files if p.endswith(marker))
        if hit:
            add(f'Teknoloji: {tech} ({len(hit)} manifest; ilk: {hit[0]})', hit[0])
    tests = sorted({p.split('/')[0] for p in files if re.search(r'(^|/)(test|tests|integration_test)/|Tests?\.cs$|_test\.\w+$', p)})
    if tests:
        add('Test konumları: ' + ', '.join(tests[:10]), None)
    for base in ('lib', 'src', 'app'):
        layers = {}
        for p in files:
            parts = p.split('/')
            if parts[0] == base and len(parts) > 2:
                layers[parts[1]] = layers.get(parts[1], 0) + 1
        if layers:
            add(f'{base}/ katmanları: ' + ', '.join(f'{k} ({v})' for k, v in sorted(layers.items(), key=lambda kv: -kv[1])[:12]), None)
    docs = sorted(p for p in files if p.endswith('.md') and (p.startswith('.ai/') or '/' not in p))
    if docs:
        add('Proje belgeleri: ' + ', '.join(docs[:15]), None)


def search(root, query, limit=10):
    f = Path(root) / '.ai' / 'memory.sqlite'
    words = re.findall(r'\w{3,}', query or '', re.U)[:12]
    if not f.is_file() or not words:
        return []
    c = db(Path(root))
    try:
        q = ' OR '.join('"' + w.replace('"', '') + '"' for w in words)
        rows = c.execute('select path, snippet(fts, 1, "[", "]", " … ", 12) from fts where fts match ? order by rank limit ?',
                         (q, limit)).fetchall()
    finally:
        c.close()
    return [{'path': p, 'snippet': s} for p, s in rows]


def resolve_import(src, dst, files):
    """Indexed file an import points at, or None (packages outside the repo, C# namespaces)."""
    lang = LANGS.get(Path(src).suffix.lower())
    if lang == 'dart' and dst.startswith('package:'):
        rest = dst[8:].split('/', 1)[-1]
        return next((f for f in files if f == 'lib/' + rest or f.endswith('/lib/' + rest)), None)
    if lang == 'python':
        stem = dst.replace('.', '/')
        return next((f for f in files if f.endswith(stem + '.py') or f.endswith(stem + '/__init__.py')), None)
    if lang in ('dart', 'ts', 'js') and not dst.startswith(('dart:', 'package:', '@')) and (dst.startswith('.') or lang == 'dart'):
        base = os.path.normpath(os.path.join(os.path.dirname(src), dst))
        return next((c for c in (base, base + '.ts', base + '.tsx', base + '.js', base + '/index.ts', base + '/index.js')
                     if c in files), None)
    return None


def dependents(root, path, depth=2):
    """Files that import `path` (transitively up to `depth`): the regression impact of changing it."""
    f = Path(root) / '.ai' / 'memory.sqlite'
    if not f.is_file() or not path:
        return []
    c = db(Path(root))
    try:
        files = {p for (p,) in c.execute('select path from files')}
        rev = {}
        for src, dst in c.execute('select src, dst from imports'):
            target = resolve_import(src, dst, files)
            if target and target != src:
                rev.setdefault(target, set()).add(src)
    finally:
        c.close()
    seen, frontier = [], [path]
    for _ in range(depth):
        frontier = sorted({d for p in frontier for d in rev.get(p, ())} - set(seen) - {path})
        seen += frontier
    return seen


def add_fact(root, kind, text, source_path=None):
    """user_decision = the user's approved rule; inference = an agent's unverified claim (never treated as a rule)."""
    if kind not in ('user_decision', 'inference'):
        raise ValueError('kind: user_decision | inference (verified kayıtlar yalnız indeksten üretilir)')
    c = db(Path(root))
    sha = c.execute('select sha from files where path=?', (source_path,)).fetchone() if source_path else None
    c.execute("insert into facts(kind,text,source_path,source_sha,status,auto,created,updated) values(?,?,?,?,'current',0,?,?)",
              (kind, text.strip(), source_path, sha[0] if sha else None, team.now(), team.now()))
    c.commit(); c.close()


def summary(root):
    root = Path(root)
    if not (root / '.ai' / 'memory.sqlite').is_file():
        return {'indexed': None, 'files': 0, 'facts': [], 'langs': {}}
    c = db(root)
    try:
        meta = dict(c.execute('select k, v from meta'))
        langs = dict(c.execute("select coalesce(nullif(lang,''),'diğer'), count(*) from files group by 1 order by 2 desc"))
        facts = [dict(zip(('id', 'kind', 'text', 'source_path', 'status', 'updated'), r)) for r in
                 c.execute('select id, kind, text, source_path, status, updated from facts order by kind, id')]
        n = c.execute('select count(*) from files').fetchone()[0]
        edges = c.execute('select count(*) from imports').fetchone()[0]
    finally:
        c.close()
    return {'indexed': meta.get('indexed'), 'git_rev': meta.get('git_rev'), 'files': n, 'imports': edges,
            'langs': langs, 'facts': facts}


# ---------------- skills ----------------

def skill_catalog():
    f = team.ROOT / 'templates' / 'skills.json'
    return [s for s in (team.read(f) if f.is_file() else []) if s.get('approved')]


def installed_skill(root, sid):
    return any((d / sid).is_dir() for d in (Path(root) / '.claude' / 'skills', Path.home() / '.claude' / 'skills'))


def suggest_skills(root, text, limit=3):
    """At most `limit` approved local skills by keyword/tech match; suggestions only, never auto-run."""
    techs = ' '.join(f['text'] for f in summary(root)['facts'] if f['text'].startswith('Teknoloji:')).lower()
    low = (text or '').lower()
    scored = []
    for s in skill_catalog():
        hits = [k for k in s.get('keywords', []) if k.lower() in low]
        tech = [t for t in s.get('tech', []) if t.lower() in techs]
        score = len(hits) * 2 + len(tech)
        if hits or (tech and not s.get('keywords')):
            scored.append((score, s, hits, tech))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, s, hits, tech in scored[:limit]:
        why = ', '.join(hits[:4]) + (' · ' + '/'.join(tech) if tech else '')
        inst = installed_skill(root, s['id'])
        out.append({'id': s['id'], 'name': s['name'], 'score': score, 'why': why,
                    'installed': inst, 'use': ('Claude Code skill olarak zaten yüklü; Skill aracıyla çağır'
                                               if inst else s.get('description', ''))})
    return out


# ---------------- shadow-mode context engine ----------------

def est_tokens(chars):
    return int(chars / 3.5)  # ponytail: char heuristic; swap for a tokenizer if decisions depend on precision


def shadow_context(root, ident, role, prompt, run_id):
    """Record the context package the runner WOULD propose; the real prompt/command are not touched."""
    root = Path(root)
    p = team.task(root, ident)
    s = team.read(p / 'state.json')
    sysprompt = team.role_prompt(root, role, ident)
    reads = ['AGENTS.md', '.ai/TEAM_RULES.md', '.ai/PROJECT.md', '.ai/OWNERSHIP.md', '.ai/CURRENT_STATE.md', f'roles/{role}.md']
    sizes = {r: (root / r).stat().st_size if (root / r).is_file() else 0 for r in reads}
    docs = {f.name: f.stat().st_size for f in sorted(p.glob('*.md'))}
    sentences = [x.strip() for x in re.split(r'(?<=[.!?])\s+|\n', prompt) if len(x.strip()) > 25]
    dup = len(sentences) - len(set(sentences))
    notes = []
    if sysprompt and sysprompt in prompt:
        notes.append(f'Rol istemi hem --append-system-prompt hem görev isteminde: ~{est_tokens(len(sysprompt))} token tekrar')
    if sizes.get('.ai/CURRENT_STATE.md', 0) > 2048:
        notes.append(f'CURRENT_STATE.md {sizes[".ai/CURRENT_STATE.md"]} B (>2 KB): eski bağlam taşınıyor olabilir')
    task_text = (p / 'TASK.md').read_text(errors='replace') if (p / 'TASK.md').is_file() else ''
    relevant = search(root, s.get('title', '') + ' ' + task_text[:2000], 8)
    if not (root / '.ai' / 'memory.sqlite').is_file():
        notes.append('Hafıza indeksi yok: ilgili dosya önerisi yapılamadı (güvenli varsayılan: ajan kendisi arar)')
    decisions = []
    if (root / '.ai' / 'memory.sqlite').is_file():
        c = db(root)
        decisions = [r[0] for r in c.execute("select text from facts where kind='user_decision' and status='current'")]
        c.close()
    original = {'prompt_chars': len(prompt), 'system_chars': len(sysprompt), 'mandatory_reads': sizes,
                'task_docs': docs}
    orig_tok = est_tokens(len(prompt) + len(sysprompt) + sum(sizes.values()) + sum(docs.values()))
    # Proposal keeps every quality/safety source (rules, role file, task docs); it only drops repetition.
    scope_files = [g for g in team.task_scope(p) or [] if not re.search(r'[*?\[]', g)]
    impact = sorted({d for g in scope_files for d in dependents(root, g)})[:20]
    proposed = {'system': 'rol istemi bir kez', 'mandatory_reads': reads, 'task_docs': list(docs),
                'relevant_files': [r['path'] for r in relevant], 'impact_files': impact, 'user_decisions': decisions}
    prop_tok = orig_tok - (est_tokens(len(sysprompt)) if sysprompt in prompt else 0)
    rec = {'mode': 'shadow', 'task': ident, 'role': role, 'run_id': run_id, 'created': team.now(),
           'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'original': original,
           'original_tokens_est': orig_tok, 'proposed': proposed, 'proposed_tokens_est': prop_tok,
           'saving_tokens_est': orig_tok - prop_tok, 'duplicate_sentences': dup, 'notes': notes}
    out = root / '.ai' / 'context-shadow' / ident / f'{role}-{team.slug(run_id)}.json'
    team.atomic(out, rec)
    return out


def shadow_records(root, ident=None):
    base = Path(root) / '.ai' / 'context-shadow'
    out = []
    for f in sorted(base.glob(f'{ident or "*"}/*.json')):
        try:
            out.append(team.read(f))
        except (OSError, ValueError):
            continue
    return out


def main():
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('--project', default=os.getcwd())
    sub = a.add_subparsers(dest='cmd', required=True)
    sub.add_parser('index'); sub.add_parser('summary')
    q = sub.add_parser('search'); q.add_argument('query')
    q = sub.add_parser('fact'); q.add_argument('kind', choices=['user_decision', 'inference']); q.add_argument('text')
    q.add_argument('--source')
    q = sub.add_parser('skills'); q.add_argument('text')
    q = sub.add_parser('shadow'); q.add_argument('--task')
    args = a.parse_args()
    root = team.project(args.project)
    out = {'index': lambda: index(root), 'summary': lambda: summary(root), 'search': lambda: search(root, args.query),
           'fact': lambda: add_fact(root, args.kind, args.text, args.source) or 'ok',
           'skills': lambda: suggest_skills(root, args.text), 'shadow': lambda: shadow_records(root, args.task)}[args.cmd]()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as e:
        print('STOP: ' + str(e), file=sys.stderr); sys.exit(2)
