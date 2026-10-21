#!/usr/bin/env python3
"""Local file-based AI team coordination. Never executes a project or remote operation."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import uuid

ROLES = {'lead': 'Claude Code', 'analyst': 'Claude Code', 'developer': 'Claude Code',
         'designer': 'Claude Code', 'tester': 'Claude Code', 'verifier': 'Codex'}
DOCS = ['TASK', 'ANALYSIS', 'DESIGN', 'IMPLEMENTATION', 'TEST_CLAUDE',
        'VERIFY_CODEX', 'DECISIONS', 'FINAL_REPORT']
STAGES = ['NEW', 'ANALYSIS', 'DESIGN', 'IMPLEMENTATION', 'TEST_CLAUDE',
          'VERIFY_CODEX', 'KNOWLEDGE_UPDATE', 'DONE']
WORKFLOWS = {
    # Narrow visual/text fixes keep both quality gates without analysis or knowledge overhead.
    'tweak': ['NEW', 'DESIGN', 'IMPLEMENTATION', 'TEST_CLAUDE',
              'VERIFY_CODEX', 'DONE'],
    'small': ['NEW', 'IMPLEMENTATION', 'TEST_CLAUDE', 'VERIFY_CODEX',
              'KNOWLEDGE_UPDATE', 'DONE'],
    'ui': ['NEW', 'DESIGN', 'IMPLEMENTATION', 'TEST_CLAUDE',
           'VERIFY_CODEX', 'KNOWLEDGE_UPDATE', 'DONE'],
    # Each profile owns its list; a shared object could be mutated through one profile.
    'full': list(STAGES),
    'risky': list(STAGES),
    # One-tester bug fix: no verifier/lead; escalates to `small` when it touches risk_paths.
    'fix': ['NEW', 'IMPLEMENTATION', 'TEST_CLAUDE', 'DONE'],
}
MAX_REWORKS = 3
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODELS={"developer":"opus","developer_small":"sonnet","tester":"sonnet","analyst":"sonnet","designer":"sonnet","lead":"sonnet"}
DEFAULT_RUNNER={"max_parallel":3,"serial_roles":["tester","verifier","lead"]}

def projects_file():
    """Machine-specific registry wins; the repo file stays as a backward-compatible fallback."""
    user=Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'ai-team'/'projects.json'
    return user if user.is_file() else ROOT/'projects.json'

def load_projects():
    f=projects_file()
    return read(f) if f.is_file() else {}

def init_project(root,dry_run=False):
    """Create the missing ai-orchestra skeleton in an existing project dir; never touches existing files."""
    root=Path(root).expanduser().resolve()
    if not root.is_dir(): raise ValueError('Not a directory: '+str(root))
    made=[];skipped=[];notes=[]
    tpl=ROOT/'templates'
    def put(rel,text=None,data=None):
        f=root/rel
        if f.exists() or f.is_symlink(): skipped.append(rel); return
        made.append(rel)
        if dry_run: return
        if data is not None: atomic(f,data)
        else: f.parent.mkdir(parents=True,exist_ok=True); f.write_text(text)
    for name,data in (('models.json',DEFAULT_MODELS),('runner.json',DEFAULT_RUNNER)): put('.ai/'+name,data=data)
    for f in sorted((tpl/'project/ai').glob('*.md')):
        text=f.read_text()
        if f.name=='PROJECT.md': text=text.replace('# Proje\n','# '+root.name+'\n',1)
        put('.ai/'+f.name,text)
    for f in sorted((tpl/'roles').glob('*.md')): put('roles/'+f.name,f.read_text())
    cm=root/'CLAUDE.md'
    if cm.is_file() and 'ai-orchestra' in cm.read_text(errors='ignore'): skipped.append('CLAUDE.md')
    elif cm.exists(): notes.append('CLAUDE.md mevcut; ai-orchestra satirini elle ekleyin: '+(tpl/'project/CLAUDE.md').read_text().split('\n')[1])
    else: put('CLAUDE.md',(tpl/'project/CLAUDE.md').read_text())
    put('AGENTS.md',(tpl/'project/AGENTS.md').read_text())
    for d in ['tasks']+['.ai/messages/'+r for r in ROLES]:
        if (root/d).is_dir(): skipped.append(d+'/')
        else:
            made.append(d+'/')
            if not dry_run: (root/d).mkdir(parents=True,exist_ok=True)
    out=['init '+str(root)+(' (dry-run, yazilmadi)' if dry_run else '')]
    out+=['  olusturuldu: '+m for m in made]+['  atlandi (var): '+m for m in skipped]+['  UYARI: '+n for n in notes]
    if not made: out.append('nothing to create')
    return '\n'.join(out)

def doctor(root=None):
    """Short environment report; returns (lines, ok)."""
    lines=[];ok=True
    def row(good,label,detail=''):
        nonlocal ok
        lines.append(('OK   ' if good else 'WARN ')+label+(' '+detail if detail else ''))
        return good
    v=sys.version_info
    ok&=row(v>=(3,10),'python3',f'{v.major}.{v.minor}.{v.micro} (>=3.10 gerekir)')
    for tool,need in (('claude',True),('codex',False),('git',True),('flutter',False),('dotnet',False)):
        w=shutil.which(tool)
        if not w and tool=='codex' and Path('/Applications/ChatGPT.app/Contents/Resources/codex').is_file(): w='/Applications/ChatGPT.app/Contents/Resources/codex'
        good=row(bool(w),tool,w or ('bulunamadı'+('' if need else ' (isteğe bağlı)')))
        if need: ok&=good
    row(shutil.which('team') is not None,'team komutu',shutil.which('team') or 'PATH\'te yok (install.sh çalıştır)')
    if root is not None:
        a=Path(root)/'.ai'
        row((a/'PROJECT.md').is_file(),'.ai/PROJECT.md',str(root))
        for n in ('models.json','runner.json'): row((a/n).is_file(),'.ai/'+n,'' if (a/n).is_file() else '(team init ile oluştur)')
        lock=a/'.runner.lock'
        if lock.exists():
            try:
                with open(lock,'a+') as h:
                    try: fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(h,fcntl.LOCK_UN); lines.append('OK   runner kilidi serbest')
                    except OSError: lines.append('INFO runner çalışıyor (kilit dolu)')
            except OSError: lines.append('WARN runner kilidi okunamadı')
        else: lines.append('OK   runner kilidi yok')
    else: lines.append('INFO aktif proje yok (--project verilmedi / .ai/PROJECT.md yok)')
    return lines,ok

def update():
    r=subprocess.run(['git','-C',str(ROOT),'pull','--ff-only'],text=True,capture_output=True)
    if r.returncode: raise ValueError('git pull başarısız: '+(r.stderr or r.stdout).strip())
    return r.stdout.strip()

def utcnow():
    # TEAM_NOW_OFFSET (seconds) shifts the clock for tests (limit retry) without real waiting.
    return dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=float(os.environ.get('TEAM_NOW_OFFSET') or 0))

def now():
    return utcnow().isoformat()

def slug(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError('Use 1-80 letters, digits, - or _; no paths.')
    return value

def read(path):
    return json.loads(path.read_text())

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.team-')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

@contextlib.contextmanager
def locked(root):
    with (root / '.ai' / '.team.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

def project(path):
    p = Path(path).expanduser().resolve()
    if not (p / '.ai/PROJECT.md').is_file():
        raise ValueError('Project is not onboarded (.ai/PROJECT.md missing).')
    return p

def task(root, ident):
    p = root / 'tasks' / slug(ident)
    if p.is_symlink() or not p.is_dir() or p.resolve().parent != (root / 'tasks').resolve():
        raise ValueError('Unknown or unsafe task path.')
    return p

def task_scope(p):
    """Globs from TASK.md '## Dosyalar' (one per line); None when absent = whole repo."""
    f = p / 'TASK.md'
    m = re.search(r'^## Dosyalar[^\n]*\n(.*?)(?=^## |\Z)', f.read_text() if f.is_file() else '', re.S | re.M)
    globs = []
    for line in (m.group(1).splitlines() if m else []):
        line = line.strip().lstrip('-*').strip().strip('`')
        if line and not line.startswith(('<!--', '#')):
            globs.append(line)
    return globs or None

def glob_re(g):
    out, i = '', 0
    while i < len(g):
        if g.startswith('**/', i): out += '(?:.*/)?'; i += 3
        elif g.startswith('**', i): out += '.*'; i += 2
        elif g[i] == '*': out += '[^/]*'; i += 1
        elif g[i] == '?': out += '[^/]'; i += 1
        else: out += re.escape(g[i]); i += 1
    return re.compile(out + '$')

def scopes_conflict(a, b):
    """Coarse overlap check on the literal glob prefixes; an unscoped task conflicts with everything."""
    if not a or not b:
        return True
    pre = lambda g: re.split(r'[*?\[]', g, 1)[0]
    return any(pre(x).startswith(pre(y)) or pre(y).startswith(pre(x)) for x in a for y in b)

def lock_conflict(root, scope, skip=None):
    """Name of another task whose active developer lock overlaps `scope`, else None."""
    for other in sorted((root / 'tasks').glob('*/state.json')):
        if other.parent.name == skip:
            continue
        if read(other).get('developer') and scopes_conflict(scope, task_scope(other.parent)):
            return other.parent.name

TEST_ROOTS = {'test', 'tests', 'integration_test'}

# Rewritten by the toolchain on every build/test; not authored source.
GENERATED = {'GeneratedPluginRegistrant.java', 'GeneratedPluginRegistrant.h',
             'GeneratedPluginRegistrant.m', 'GeneratedPluginRegistrant.swift',
             '.flutter-plugins', '.flutter-plugins-dependencies',
             'Generated.xcconfig', 'flutter_export_environment.sh'}


def production_snapshot(root, scope=None):
    """Hash production source only, leaving test directories out.

    The verifier must add its own independent tests, which changes the full
    snapshot. Gating Codex verification on that value deadlocked the role: it
    could never record the defect it had just found. Production code is what
    both test stages must agree on; each stage's tests are its own evidence.
    """
    return snapshot(root, skip_tests=True, scope=scope)


def snapshot(root, skip_tests=False, scope=None):
    """Hash existing source, including dirty/untracked files; exclude coordination and build outputs.

    `scope` (task globs) limits hashing to matching files so other tasks' edits do not invalidate evidence.
    """
    scope_res = [glob_re(g) for g in scope] if scope else None
    digest = hashlib.sha256()
    # Tool/policy/config changes invalidate previous evidence too.
    digest.update(Path(__file__).read_bytes())
    for policy in ['.ai/project.json','.ai/TEAM_RULES.md','AGENTS.md','CLAUDE.md']:
        f=root/policy
        if f.is_file():digest.update(f.read_bytes())
    excluded = {'.git', '.ai', '.cmux', '.claude', '.codex', 'roles', 'tasks',
                'build', '.dart_tool', 'node_modules', 'Pods', '.vs', '.idea',
                'bin', 'obj', '__pycache__', '.swiftpm',
                # Gradle/Kotlin build caches change on every Android build.
                '.gradle', '.kotlin', '.cxx',
                # Runtime logs (Serilog under src/Host/*/logs) grow while an app runs.
                'logs',
                # A literal '~' directory is tool state written by mistake, never source.
                '~', 'ephemeral'}
    if skip_tests:
        excluded = excluded | set(read(root / '.ai/project.json').get('test_roots', TEST_ROOTS))
    cfg = read(root / '.ai/project.json')
    for base in sorted(cfg.get('source_roots', [str(root)])):
        b = Path(base)
        if not b.is_dir():
            raise ValueError('Source root unavailable: ' + base)
        digest.update(base.encode())
        tracked = None
        for folder, dirs, files in os.walk(b):
            dirs[:] = sorted(d for d in dirs if d not in excluded and not (Path(folder)/d).is_symlink())
            for name in sorted(files):
                p = Path(folder)/name
                # Toolchain-generated files are rewritten by any build or test run.
                # Counting them made a verifier invalidate its own evidence the
                # moment it ran the tests it was asked to run.
                if name in GENERATED or name in {'AGENTS.md', 'CLAUDE.md', '.DS_Store'} or p.is_symlink():
                    continue
                if scope_res and not any(r.match('/'.join(p.relative_to(b).parts)) for r in scope_res):
                    continue
                # Xcode rewrites per-user xcuserdata and a workspace's Package.resolved on
                # build/test; only untracked copies are tool state, tracked ones stay source.
                parts = p.relative_to(b).parts
                if 'xcuserdata' in parts[:-1] or (
                        name == 'Package.resolved' and len(parts) >= 4 and parts[-4].endswith('.xcworkspace')
                        and parts[-3:-1] == ('xcshareddata', 'swiftpm')):
                    if tracked is None:
                        r = subprocess.run(['git', '-C', str(b), 'ls-files', '-z'], capture_output=True)
                        tracked = set(r.stdout.decode(errors='replace').split('\0')) if r.returncode == 0 else set()
                    if '/'.join(parts) not in tracked:
                        continue
                # Hash bytes without exposing contents. Config changes also invalidate tests.
                digest.update(str(p.relative_to(b)).encode())
                with p.open('rb') as f:
                    for block in iter(lambda: f.read(1024*1024), b''):
                        digest.update(block)
    return digest.hexdigest()

# Coordination files are never part of a task's source change set.
COORD_DIRS = ('.ai/', 'tasks/', 'roles/')
CLOSED_STAGES = ('DONE', 'CLOSED', 'ABANDONED')

def git_changes(root):
    """{path: content hash} of every dirty/untracked file under root, paths relative to root; {} without git."""
    r = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '-z', '--untracked-files=all', '--', '.'],
                       capture_output=True)
    pre = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-prefix'], capture_output=True, text=True)
    if r.returncode or pre.returncode:
        return {}
    prefix, out = pre.stdout.strip(), {}
    items = r.stdout.decode(errors='replace').split('\0')
    i = 0
    while i < len(items):
        entry = items[i]; i += 1
        if len(entry) < 4:
            continue
        paths = [entry[3:]]
        if entry[0] in 'RC':  # rename/copy: the next entry is the old path
            paths.append(items[i]); i += 1
        for path in paths:
            rel = path[len(prefix):] if path.startswith(prefix) else None
            if not rel or rel.startswith(COORD_DIRS):
                continue
            f = root / rel
            # ponytail: hashes every dirty file each time; fine unless huge untracked trees are not gitignored.
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() and not f.is_symlink() else '-'
    return out

def add_changed(p, s, before, after):
    """Accumulate files whose dirty content differs between two git_changes() snapshots (task scope only)."""
    files = {f for f in before.keys() | after.keys() if before.get(f) != after.get(f)}
    scope = task_scope(p)
    if scope:
        res = [glob_re(g) for g in scope]
        files = {f for f in files if any(r.match(f) for r in res)}
    s['changed_files'] = sorted(set(s.get('changed_files') or []) | files)
    return s['changed_files']

def claim_baseline(root, ident):
    return root / '.ai' / 'runner-logs' / ident / 'claim-baseline.json'

def task_title(p, s):
    f = p / 'TASK.md'
    head = next((l[2:] for l in (f.read_text().splitlines() if f.is_file() else []) if l.startswith('# ')), '')
    head = head.replace(p.name, '', 1).strip(' -—:')
    return s.get('title') or p.name if head in ('', 'TASK') else head

def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)

def commit(root, ident, apply=False, force=False):
    """Per-task commit proposal; DRY-RUN unless apply. Never pushes."""
    p = task(root, ident); s = read(p / 'state.json')
    dirty = git_changes(root)
    files = [f for f in s.get('changed_files') or [] if f in dirty]
    msg = f'{ident}: {task_title(p, s)}'
    out = []
    closed = s['stage'] in ('DONE', 'CLOSED')
    if not closed:
        out.append(f'UYARI: görev {s["stage"]} aşamasında (DONE/CLOSED değil); --apply için --force gerekir.')
    if not files:
        return '\n'.join(out + ['Commit edilecek değişiklik yok (changed_files boş ya da dosyalar temiz).'])
    out += ['Dosyalar:'] + ['  ' + f for f in files]
    for other in sorted((root / 'tasks').glob('*/state.json')):
        if other.parent.name == ident:
            continue
        try: o = read(other)
        except (OSError, ValueError): continue
        both = sorted(set(files) & set(o.get('changed_files') or []))
        if both and o.get('stage') not in CLOSED_STAGES:
            out.append(f'UYARI: açık görev {other.parent.name} aynı dosyalara dokundu: ' + ', '.join(both))
    cmds = [['git', '-C', str(root), 'add', '--', *files], ['git', '-C', str(root), 'commit', '-m', msg]]
    out += ['Mesaj: ' + msg, 'Komutlar:'] + ['  ' + shlex.join(c) for c in cmds]
    if not apply:
        return '\n'.join(out + ['DRY-RUN: hiçbir şey çalıştırılmadı; uygulamak için --apply (push yapılmaz).'])
    if not closed and not force:
        raise ValueError('Görev DONE/CLOSED değil; yine de commit için --force ver.')
    staged = git(root, 'diff', '--cached', '--name-only', '--relative')
    extra = sorted(set(staged.stdout.split()) - set(files))
    if staged.returncode or extra:
        raise ValueError('Başka staged değişiklik var, commit yapılmadı: ' + ', '.join(extra or ['git hatası']))
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode:
            raise ValueError('git başarısız: ' + (r.stderr or r.stdout).strip())
    head = git(root, 'rev-parse', 'HEAD').stdout.strip()
    with locked(root):
        s = read(p / 'state.json')
        s.update(commit=head, updated=now(), revision=s.get('revision', 0) + 1)
        atomic(p / 'state.json', s)
    return '\n'.join(out + ['Commit: ' + head + ' (push yok)'])

def knowledge_hashes(root):
    # CHANGELOG.md is appended by every closing fix task; it must not invalidate other tasks' knowledge ack.
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root/'.ai').glob('*.md')) if p.name!='CHANGELOG.md'}

def runner_json(root):
    """Optional .ai/runner.json; {} when missing or unreadable."""
    try: return read(root/'.ai/runner.json')
    except (OSError,ValueError): return {}

def risk_files(root, files):
    res=[glob_re(g) for g in runner_json(root).get('risk_paths') or []]
    return [f for f in files if any(r.match(f) for r in res)]

UI_WORDS=re.compile(r'\b(tasarım|tasarim|ekran|ui)\b',re.I)

def suggest_profile(root, p=None, title=''):
    """(profile, reason) for `--profile auto`."""
    text=title+'\n'+((p/'TASK.md').read_text() if p and (p/'TASK.md').is_file() else '')
    if UI_WORDS.search(text): return 'ui','TASK.md/başlık tasarım/ekran/UI içeriyor'
    scope=task_scope(p) if p else None
    risk=runner_json(root).get('risk_paths') or []
    if not scope: return 'small','## Dosyalar kapsamı yok'
    if len(scope)>3: return 'small',f'kapsam {len(scope)} glob (>3)'
    if risk and scopes_conflict(scope,risk): return 'small','kapsam risk_paths ile kesişiyor'
    return 'fix',f'kapsam {len(scope)} glob, risk_paths dışı'

def append_changelog(root, s):
    f=root/'.ai/CHANGELOG.md'
    result=(s.get('claude_test') or {}).get('result','?')
    line=f"- {now()[:10]} {s['task']} — {s.get('title','')} — {result}\n"
    with f.open('a') as h:
        if h.tell()==0: h.write('# CHANGELOG\n\n')
        h.write(line)

def reclassification_stage(state, new_path):
    """Keep completed workflow work when the new profile can represent it."""
    current=state['stage']
    if current in new_path:
        return current
    old_path=state.get('stage_path') or WORKFLOWS.get(state.get('profile','full'),STAGES)
    # Missing stages roll back only as far as the closest earlier shared checkpoint.
    if current in old_path:
        earlier=old_path[:old_path.index(current)]
    elif current in STAGES:
        earlier=STAGES[:STAGES.index(current)]
    else:
        earlier=[]
    return next((stage for stage in reversed(earlier) if stage in new_path),new_path[0])

def create(root, ident, title, profile='full', auto=False, files=None):
    ident = slug(ident)
    choice = None
    if profile == 'auto':
        scratch = Path(tempfile.mkdtemp())
        try:
            if files: (scratch/'TASK.md').write_text('## Dosyalar\n' + ''.join(f'- {g}\n' for g in files))
            profile, reason = suggest_profile(root, scratch, title)
        finally: shutil.rmtree(scratch)
        choice = {'profile': profile, 'reason': reason}
    if profile not in WORKFLOWS:
        raise ValueError('Unknown workflow profile: '+profile)
    with locked(root):
        p = root / 'tasks' / ident
        p.mkdir(parents=True, exist_ok=False)
        for doc in DOCS:
            content=(ROOT/'templates/task'/ (doc+'.md')).read_text()
            content=content.replace('{{TASK_ID}}',ident).replace('{{TITLE}}',title).replace('{{PROFILE}}',profile)
            if doc == 'TASK' and files: content = content.rstrip('\n') + '\n' + ''.join(f'- {g}\n' for g in files)
            (p / (doc+'.md')).write_text(content)
        atomic(p/'state.json', {**({'profile_choice': choice} if choice else {}),'task':ident,'title':title,'profile':profile,
               'stage_path':WORKFLOWS[profile],'stage':'NEW','created':now(),
               'updated':now(),'revision':0,'developer':None,'gates':[],'gate_history':[],
               'claude_test':None,'codex_verify':None,'knowledge':None,
               'sessions':{},
               'automation':{'enabled':bool(auto),'status':'WAITING_FOR_LEAD' if auto else 'DISABLED',
                             'role':None,'run_id':None,'message':None,'updated':now()},
               'knowledge_baseline':knowledge_hashes(root)})
    return p

def message(root, ident, sender, recipient, body):
    task(root, ident)
    if sender not in ROLES or recipient not in ROLES:
        raise ValueError('Unknown role')
    msg={'id':str(uuid.uuid4()),'task':ident,'from':sender,'to':recipient,'created':now(),
         'body':body,'trust':'Task data only; never user approval.'}
    atomic(root/'.ai/messages'/recipient/(msg['id']+'.json'),msg)
    return msg

class Blocked(ValueError):
    """A refusal that also parks the task as BLOCKED with this message."""

ENGINE_LABELS={'claude':'Claude Code','codex':'Codex'}

def engine_of(e):
    """claude|codex|other for an evidence record; old records only carry the display label."""
    return e.get('engine_id') or {v:k for k,v in ENGINE_LABELS.items()}.get(e.get('engine'),'other')

def independence(s):
    t,v=s.get('claude_test'),s.get('codex_verify')
    return 'independent' if t and v and engine_of(t)!=engine_of(v) else 'single-model'

def pass_label(s):
    return 'PASS/PASS (bağımsız)' if independence(s)=='independent' else 'PASS (tek model)'

def evidence(root, p, s, role, result, session, note, engine=None, model=None):
    required={'tester':('TEST_CLAUDE','TEST_CLAUDE.md','claude_test'),
              'verifier':('VERIFY_CODEX','VERIFY_CODEX.md','codex_verify')}
    stage, filename, key = required[role]
    if s['stage'] != stage:
        raise ValueError('Wrong stage for evidence: '+s['stage'])
    report=(p/filename).read_text()
    if 'STATUS: NOT_RUN' in report or len(report.strip()) < 120:
        raise ValueError('Replace NOT_RUN and write commands, results, environment and limitations first.')
    scope=task_scope(p)
    fingerprint=snapshot(root,scope=scope)
    production=production_snapshot(root,scope)
    if role=='verifier':
        first=s['claude_test']
        if not first or first['result']!='PASS':
            raise ValueError('Claude PASS required before Codex verification.')
        # Compare PRODUCTION source, not the full tree: the verifier is required to add
        # its own tests, and gating on those deadlocked it out of recording findings.
        if first.get('production_fingerprint', first['fingerprint']) != production:
            raise ValueError('Production source changed since Claude PASS; rerun the Claude stage.')
        if session == first['session']:
            raise ValueError('Independent Codex session required.')
    # The runner exports the real engine/model of the role process; manual calls may pass them.
    default='codex' if role=='verifier' and os.environ.get('TEAM_VERIFIER_ENGINE')!='claude' else 'claude'
    engine=engine or os.environ.get('AI_TEAM_ENGINE') or default
    s[key]={'engine':ENGINE_LABELS.get(engine,engine),'engine_id':engine,
            'model':model or os.environ.get('AI_TEAM_MODEL') or None,
            'role':role,'session':session,'result':result,
            'fingerprint':fingerprint,'production_fingerprint':production,
            'created':now(),'note':note,
            'report_sha256':hashlib.sha256(report.encode()).hexdigest()}

def advance(root,p,s,stage):
    if s['developer']:
        raise ValueError('Release developer source lock before changing stage.')
    path=s.get('stage_path') or WORKFLOWS.get(s.get('profile','full'),STAGES)
    if stage not in path or s['stage'] not in path or path.index(stage)!=path.index(s['stage'])+1:
        raise ValueError('Advance one stage in the selected profile; use rework or reclassify when needed.')
    if stage in ['VERIFY_CODEX','KNOWLEDGE_UPDATE','DONE']:
        # Production source must be unchanged since each stage signed off. Test files
        # are excluded: every test stage adds its own, and requiring the whole tree to
        # stay byte-identical made the later stages impossible to satisfy.
        fp=production_snapshot(root,task_scope(p))
        for key,file in [('claude_test','TEST_CLAUDE.md')]+([('codex_verify','VERIFY_CODEX.md')] if stage!='VERIFY_CODEX' and 'VERIFY_CODEX' in path else []):
            e=s[key]
            if not e or e['result']!='PASS' or e.get('production_fingerprint',e['fingerprint'])!=fp or e['report_sha256']!=hashlib.sha256((p/file).read_bytes()).hexdigest():
                raise ValueError('Missing, failed or stale evidence: '+key)
    if stage=='DONE' and s.get('profile')=='fix':
        risky=risk_files(root,s.get('changed_files') or [])
        if risky:
            # A fix that touched risk_paths gets the independent verifier and knowledge stages of `small`.
            s.update(profile='small',stage_path=WORKFLOWS['small'],stage='VERIFY_CODEX',
                     escalated={'reason':'risk_paths','files':risky,'at':now()})
            return
    if stage=='DONE':
        if any(g['status']!='RESOLVED_BY_LEAD' for g in s['gates']):
            raise ValueError('Unresolved human gate.')
        # A risky task must show the human gate it was classified for; silence is not evidence.
        if s.get('profile')=='risky' and not s['gates']:
            raise ValueError('Risky profile requires a recorded human gate before DONE.')
        # Profiles without KNOWLEDGE_UPDATE may close after the same two independent test gates.
        if 'KNOWLEDGE_UPDATE' in path:
            if not s['knowledge'] or s['knowledge']['fingerprint']!=snapshot(root,scope=task_scope(p)) or s['knowledge']['documents']!=knowledge_hashes(root):
                raise ValueError('Knowledge update acknowledgement required on same source.')
        if 'VERIFY_CODEX' in path and 'STATUS: NOT_RUN' in (p/'FINAL_REPORT.md').read_text():
            raise ValueError('Final report is not written.')
        if (runner_json(root).get('require_independent_verifier') and s.get('profile')!='fix'
                and independence(s)=='single-model'):
            raise Blocked('Bağımsız doğrulayıcı zorunlu (runner.json require_independent_verifier): tester ve '
                          'verifier aynı motor; verifier\'ı farklı motorla yeniden çalıştır.')
        s['independence']=independence(s)
    s['stage']=stage
    if stage=='DONE':
        s['done_at']=now()
        if s.get('profile')=='fix': append_changelog(root,s)

STATUS_GROUPS=('Seni bekliyor','Çalışıyor','Limit bekliyor','Sırada (auto)','Commit bekliyor')

def status_summary(root):
    """Short grouped view of what needs attention; closed tasks are hidden."""
    groups={g:[] for g in STATUS_GROUPS}
    for f in sorted((root/'tasks').glob('*/state.json')):
        ident=f.parent.name
        try: s=read(f)
        except (OSError,ValueError) as e:
            groups['Seni bekliyor'].append(f'{ident} ? — state okunamadı: {e}'); continue
        a=s.get('automation') or {}; st=s.get('stage','?'); status=a.get('status')
        row=lambda m: f'{ident} {st} — {" ".join(str(m or "").split())[:90]}'.rstrip(' —')
        if st=='DONE':
            if s.get('changed_files') and not s.get('commit'):
                groups['Commit bekliyor'].append(row(f'{len(s["changed_files"])} dosya · {pass_label(s)} · team commit {ident}'))
            continue
        if st in CLOSED_STAGES: continue
        # WAITING_FOR_LEAD is stale once Lead advanced past NEW; the runner refreshes it on its next round.
        if st=='NEW' or status in ('AWAITING_USER','BLOCKED') or not a.get('enabled'):
            groups['Seni bekliyor'].append(row(a.get('message') if a.get('enabled') else (a.get('message') or 'otomasyon kapalı')))
        elif status=='RUNNING': groups['Çalışıyor'].append(row(f'{a.get("role")} · {a.get("message") or ""}'))
        elif status=='WAITING_LIMIT': groups['Limit bekliyor'].append(row(f'retry_at {str(a.get("retry_at"))[:16]} · {a.get("message") or ""}'))
        else: groups['Sırada (auto)'].append(row(a.get('message') if status!='WAITING_FOR_LEAD' else 'runner bekliyor'))
    out=[]
    for g,rows in groups.items():
        if rows: out+=[f'{g} ({len(rows)})']+['  '+r for r in rows]
    return '\n'.join(out) or 'Açık görev yok.'

def metric(root, row):
    """Append one line to .ai/metrics.jsonl (O_APPEND, no lock: callers may already hold the team lock)."""
    try:
        with (root/'.ai'/'metrics.jsonl').open('a') as f:
            f.write(json.dumps({'ts':now(),**row},ensure_ascii=False)+'\n')
    except OSError:
        pass  # metrics never break the workflow

def load_metrics(root):
    f=root/'.ai'/'metrics.jsonl'; rows=[]
    for line in (f.read_text(errors='replace').splitlines() if f.is_file() else []):
        try: rows.append(json.loads(line))
        except ValueError: continue
    return rows

def log_usage(path):
    """Token usage from a role log: Claude stream-json `result` or Codex `turn.completed` events.

    `input` is uncached input on both engines (Codex input_tokens includes cached_input_tokens).
    Codex logs carry no cost, so cost_usd stays None for them.
    """
    tok={'input':0,'output':0,'cache_read':0,'cache_creation':0}; out={'tokens':None,'cost_usd':None,'model':None,'duration_s':None}
    try: lines=Path(path).read_text(errors='replace').splitlines()
    except OSError: return out
    for line in lines:
        if not line.startswith('{'): continue
        try: e=json.loads(line)
        except ValueError: continue
        u=e.get('usage') if isinstance(e,dict) else None
        if not isinstance(u,dict) or e.get('type') not in ('result','turn.completed'): continue
        out['tokens']=tok
        if e['type']=='result':
            tok['input']+=u.get('input_tokens') or 0; tok['output']+=u.get('output_tokens') or 0
            tok['cache_read']+=u.get('cache_read_input_tokens') or 0; tok['cache_creation']+=u.get('cache_creation_input_tokens') or 0
            if e.get('total_cost_usd') is not None: out['cost_usd']=(out['cost_usd'] or 0)+e['total_cost_usd']
            if e.get('duration_ms'): out['duration_s']=(out['duration_s'] or 0)+e['duration_ms']/1000
            out['model']=next(iter(e.get('modelUsage') or {}),out['model'])
        else:
            cached=u.get('cached_input_tokens') or 0
            tok['input']+=max((u.get('input_tokens') or 0)-cached,0); tok['output']+=u.get('output_tokens') or 0
            tok['cache_read']+=cached; tok['cache_creation']+=u.get('cache_write_input_tokens') or 0
    return out

def _minutes(seconds):
    return f'{seconds/60:.1f} dk'

def report(root, days=None, ident=None):
    since=(utcnow()-dt.timedelta(days=days)).isoformat() if days else ''
    rows=[r for r in load_metrics(root) if (r.get('ts_start') or r.get('ts') or '')>=since and (not ident or r.get('task')==ident)]
    runs=[r for r in rows if r.get('type')=='run']
    closed=[]
    for f in sorted((root/'tasks').glob('*/state.json')):
        try: s=read(f)
        except (OSError,ValueError): continue
        at=s.get('done_at') or (s.get('closed') or {}).get('at') or s.get('updated') or ''
        if s.get('stage') in CLOSED_STAGES and at>=since and (not ident or f.parent.name==ident): closed.append((s,at))
    if not runs and not closed:
        return 'Rapor: veri yok (henüz .ai/metrics.jsonl satırı ya da kapanan görev yok).'
    out=[f'Rapor: {"son "+str(days)+" gün" if days else "tüm zamanlar"}'+(f' · görev {ident}' if ident else '')]
    by_profile={}
    for s,_ in closed: by_profile[s.get('profile','?')]=by_profile.get(s.get('profile','?'),0)+1
    stages={k:sum(s['stage']==k for s,_ in closed) for k in CLOSED_STAGES}
    out.append(f'Kapanan görev: {len(closed)} ('+', '.join(f'{k} {v}' for k,v in sorted(by_profile.items()))+') · '
               +', '.join(f'{k} {v}' for k,v in stages.items()))
    spans=[]
    for s,at in closed:
        try:
            if s['stage']=='DONE': spans.append((dt.datetime.fromisoformat(at)-dt.datetime.fromisoformat(s['created'])).total_seconds())
        except (KeyError,TypeError,ValueError): pass
    if spans: out.append(f'NEW→DONE: ort {_minutes(statistics.mean(spans))}, medyan {_minutes(statistics.median(spans))} ({len(spans)} görev)')
    roles={}
    for r in runs: roles.setdefault(r.get('role','?'),[]).append(r.get('duration_s') or 0)
    if roles: out.append('Rol süresi (ort/medyan): '+' · '.join(
        f'{k} {_minutes(statistics.mean(v))}/{_minutes(statistics.median(v))} ({len(v)})' for k,v in sorted(roles.items())))
    table={}
    for r in runs:
        k=(r.get('role','?'),r.get('model') or '-'); a=table.setdefault(k,[0,0,0,0,0,None])
        a[0]+=1
        for i,key in enumerate(('input','output','cache_read','cache_creation'),1): a[i]+=(r.get('tokens') or {}).get(key) or 0
        if r.get('cost_usd') is not None: a[5]=(a[5] or 0)+r['cost_usd']
    if table:
        out.append(f'{"rol":<10} {"model":<18} {"çalışma":>7} {"input":>10} {"output":>10} {"cache_read":>12} {"cache_write":>11} {"$":>8}')
        for (role,model),a in sorted(table.items()):
            out.append(f'{role:<10} {model[:18]:<18} {a[0]:>7} {a[1]:>10} {a[2]:>10} {a[3]:>12} {a[4]:>11} '+(f'{a[5]:>8.2f}' if a[5] is not None else f'{"-":>8}'))
        cost=sum(a[5] or 0 for a in table.values()); out.append(f'Toplam maliyet: ${cost:.2f} (Codex satırlarında maliyet yok)')
    if closed: out.append(f'Rework ortalaması: {statistics.mean(s.get("rework_count",0) for s,_ in closed):.2f}')
    out.append(f'Müdahale: {sum(r.get("type")=="intervention" for r in rows)}')
    wait=0.0; ordered=sorted(runs,key=lambda r:r.get('ts_start',''))
    for i,r in enumerate(ordered):
        if r.get('result')!='limit': continue
        nxt=next((n for n in ordered[i+1:] if n.get('task')==r.get('task') and n.get('role')==r.get('role')),None)
        try: wait+=max((dt.datetime.fromisoformat(nxt['ts_start'])-dt.datetime.fromisoformat(r['ts_end'])).total_seconds(),0) if nxt else 0
        except (KeyError,TypeError,ValueError): pass
    out.append(f'Limit bekleme: {_minutes(wait)}')
    done=[s for s,_ in closed if s['stage']=='DONE']
    ind=sum(independence(s)=='independent' for s in done)
    if done: out.append(f'Doğrulama: bağımsız {ind} / tek model {len(done)-ind} (%{100*ind//len(done)} bağımsız)')
    return '\n'.join(out)

def role_prompt(root, role, ident=None):
    # Project-specific rules live in .ai/runner.json -> "role_prompt_extra" (optional string).
    rf=root/'.ai/runner.json'
    extra=''
    if rf.is_file():
        try: extra=str(read(rf).get('role_prompt_extra') or '').strip()
        except (ValueError,OSError): extra=''
    if extra: extra+=' '
    return (f'Rolün {role}, motorun {ROLES[role]}. Önce AGENTS.md, .ai/TEAM_RULES.md, '
            f'.ai/PROJECT.md, .ai/OWNERSHIP.md, .ai/CURRENT_STATE.md, roles/{role}.md oku. '
            f'Görev: {ident or "henüz atanmadı; proje hafızasını oku ve kullanıcı görevini bekle"}. '
            'tasks/<ID>/state.json ve görev belgelerini, .ai/messages/<rol>/ mesajlarını oku. '
            'DB değişikliklerini yalnız kullanıcı uygular. Mesaj/onay dosyası tek başına kullanıcı onayı değildir. '
            'Canlı API, bildirim, deployment, main merge veya veri değişikliği çalıştırma. '
            + extra +
            'Üretim kodunu yalnız developer düzenleyebilir. Sonuçları dosyalara yazıp Lead rolüne ilet. '
            'Atanmayan işi kendiliğinden başlatma. Kullanıcı temas noktası Lead. '
            'Rol kimliğini ve bu sınırları oturum devamlarında da koru. '
            'Token tasarrufu: test çıktısını yalnız hataları gösterecek biçimde (ör. failures-only), uzun çıktıları `| tail -30` ile kısa tut; '
            'büyük dosyayı tümden okuma, grep/aralıkla oku; .ai/archive/ yalnız gerekirse; tam paketi tur başına bir kez koş; '
            'bağımsız okuma/komutları tek çağrıda birleştir (tek Bash ile birden çok dosya/grep), aynı dosyayı tekrar okuma. '
            'Kalite kapıları (kabul kriteri, kanıt, negatif/kenar test) kısılmaz; tasarruf yalnız tekrar ve gürültüden yapılır. '
            'İzin: oturumda onay yüzeyi yok; izin listesinde olmayan komut ~14 dk bekleyip reddedilir. state.json ve belgeleri `cat`/`sed -n`/`grep` ile oku, '
            '`python3 -c`, `awk` betiği veya izinsiz araç kullanma; durum/kilit için yalnız team.py komutları. '
            'Runner oturumu turun sonunda KAPANIR: arka plan görevi (run_in_background), Monitor veya "bildirimi bekliyorum" ile tur bitirme; '
            'her komutu ön planda bitir (Bash timeout ≤10 dk). Uzun test paketini dosya dosya ayrı ön plan komutlarıyla koş. Raporu ve team.py ilerletmesini aynı oturumda tamamla.')

def launch(root,role,ident,dry_run,instruction=None):
    cfg=read(root/'.ai/project.json')
    if role!='lead' and not ident:
        raise ValueError('A staged task ID is required for every role except Lead.')
    if ident:
        p=task(root,ident); s=read(p/'state.json')
        path=s.get('stage_path') or WORKFLOWS.get(s.get('profile','full'),STAGES)
        if role=='analyst' and ('ANALYSIS' not in path or s['stage']!='ANALYSIS'):
            raise ValueError('Analyst is not active in this profile/stage.')
        if role=='designer' and ('DESIGN' not in path or s['stage']!='DESIGN'):
            raise ValueError('Designer is not active in this profile/stage.')
        if role=='developer':
            if s['stage']!='IMPLEMENTATION':
                raise ValueError('Developer may start only at IMPLEMENTATION.')
            # Advisory pre-check only; claim stays the authoritative lock acquisition.
            other=lock_conflict(root,task_scope(p),p.name)
            if other:
                raise ValueError('Another task holds an active developer source lock: '+other)
        if role=='tester' and s['stage']!='TEST_CLAUDE':
            raise ValueError('Tester may start only at TEST_CLAUDE.')
        if role=='verifier' and s['stage']!='VERIFY_CODEX':
            raise ValueError('Verifier may start only after Claude PASS.')
    prompt=role_prompt(root,role,ident)
    if ROLES[role]=='Codex':
        cmd=['codex','-C',str(root),'-s','workspace-write','-a','on-request',prompt]
    else:
        cmd=['claude','--permission-mode','acceptEdits',
             '--allowedTools',
             f'Read,Grep,Glob,Write,Edit,Bash(python3 {Path(__file__).resolve()} *)',
             '--append-system-prompt',prompt,
             instruction or 'Görev durumunu ve mesaj kutunu inceleyerek atanmış işi bu turda tamamla.']
    executable = cmd[0]
    binary = shutil.which(executable)
    if not binary and executable=='codex':
        fallback=Path('/Applications/ChatGPT.app/Contents/Resources/codex')
        if fallback.is_file(): binary=str(fallback)
    if not binary:
        raise ValueError(executable+' executable not found.')
    cmd[0]=binary
    if os.environ.get('CMUX_SURFACE_ID'):
        wrapper=Path('/Applications/cmux.app/Contents/Resources/bin')/('cmux-'+executable+'-wrapper')
        if wrapper.is_file():
            os.environ['CMUX_CUSTOM_'+executable.upper()+'_PATH']=binary
            cmd[0]=str(wrapper)
    if dry_run:
        return shlex.join(cmd)
    os.chdir(root)
    os.environ['AI_TEAM_ROLE']=role
    os.environ['AI_TEAM_PROJECT']=cfg['id']
    if ident: os.environ['AI_TEAM_TASK']=ident
    os.execvp(cmd[0],cmd)

def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--project',default=os.getcwd())
    sub=a.add_subparsers(dest='cmd',required=True)
    q=sub.add_parser('new'); q.add_argument('id'); q.add_argument('title'); q.add_argument('--profile',choices=[*WORKFLOWS,'auto'],default='full'); q.add_argument('--auto',action='store_true'); q.add_argument('--files',action='append',help='## Dosyalar globu (tekrarlanabilir)')
    q=sub.add_parser('status'); q.add_argument('id',nargs='?'); q.add_argument('--all',action='store_true',help='eski tam JSON görünümü')
    q=sub.add_parser('message'); q.add_argument('id'); q.add_argument('--from',dest='sender',choices=ROLES,required=True); q.add_argument('--to',choices=ROLES,required=True); q.add_argument('--body',required=True)
    q=sub.add_parser('inbox'); q.add_argument('role',choices=ROLES); q.add_argument('--task')
    q=sub.add_parser('advance'); q.add_argument('id'); q.add_argument('stage',choices=STAGES)
    q=sub.add_parser('rework'); q.add_argument('id'); q.add_argument('--reason',required=True)
    q=sub.add_parser('reclassify'); q.add_argument('id'); q.add_argument('profile',choices=[*WORKFLOWS,'auto']); q.add_argument('--reason',required=True)
    q=sub.add_parser('claim'); q.add_argument('id'); q.add_argument('--session',required=True)
    q=sub.add_parser('release'); q.add_argument('id'); q.add_argument('--session',required=True)
    q=sub.add_parser('evidence'); q.add_argument('id'); q.add_argument('--role',choices=['tester','verifier'],required=True); q.add_argument('--result',choices=['PASS','FAIL','BLOCKED'],required=True); q.add_argument('--session',required=True); q.add_argument('--note',required=True)
    q.add_argument('--engine',choices=['claude','codex','other']); q.add_argument('--model')
    q=sub.add_parser('knowledge'); q.add_argument('id'); q.add_argument('--summary',required=True)
    q=sub.add_parser('gate'); q.add_argument('id'); q.add_argument('--kind',choices=['DB','HIGH_IMPACT','UNKNOWN_IMPACT'],required=True); q.add_argument('--action',required=True); q.add_argument('--target',required=True); q.add_argument('--environment',required=True)
    q=sub.add_parser('resolve-gate'); q.add_argument('id'); q.add_argument('gate_id'); q.add_argument('--user-message-ref',required=True); q.add_argument('--outcome',choices=['cancelled','user-applied','explicitly-approved'],required=True)
    q=sub.add_parser('launch'); q.add_argument('role',choices=ROLES); q.add_argument('--task'); q.add_argument('--dry-run',action='store_true'); q.add_argument('--instruction')
    q=sub.add_parser('automate'); q.add_argument('id'); mode=q.add_mutually_exclusive_group(required=True); mode.add_argument('--enable',action='store_true'); mode.add_argument('--disable',action='store_true'); mode.add_argument('--resume',action='store_true')
    q=sub.add_parser('close'); q.add_argument('id'); q.add_argument('--reason',required=True); q.add_argument('--abandon',action='store_true')
    q=sub.add_parser('report'); q.add_argument('--days',type=float); q.add_argument('--task')
    q=sub.add_parser('commit'); q.add_argument('id'); q.add_argument('--apply',action='store_true'); q.add_argument('--force',action='store_true')
    sub.add_parser('doctor'); q=sub.add_parser('init'); q.add_argument('path',nargs='?'); q.add_argument('--dry-run',action='store_true'); sub.add_parser('update')
    args=a.parse_args()
    if args.cmd=='init': print(init_project(args.path or args.project,args.dry_run)); return
    if args.cmd=='update': print(update()); return
    if args.cmd=='doctor':
        pr=Path(args.project).expanduser().resolve()
        lines,good=doctor(pr if (pr/'.ai/PROJECT.md').is_file() else None)
        print('\n'.join(lines)); sys.exit(0 if good else 1)
    root=project(args.project)
    if args.cmd=='new':
        p=create(root,args.id,args.title,args.profile,args.auto,args.files)
        c=read(p/'state.json').get('profile_choice')
        if c: print(f"Profil: {c['profile']} (auto) — {c['reason']}")
        print(p); return
    if args.cmd=='report': print(report(root,args.days,args.task)); return
    if args.cmd=='commit': print(commit(root,args.id,args.apply,args.force)); return
    if args.cmd=='launch': print(launch(root,args.role,args.task,args.dry_run,args.instruction)); return
    if args.cmd=='message': print(json.dumps(message(root,args.id,args.sender,args.to,args.body),ensure_ascii=False)); return
    if args.cmd=='inbox':
        for f in sorted((root/'.ai/messages'/args.role).glob('*.json')):
            m=read(f)
            if not args.task or m['task']==args.task: print(json.dumps(m,ensure_ascii=False))
        return
    if args.cmd=='status' and not args.id and not args.all:
        print(status_summary(root)); return
    if args.cmd=='status':
        for p in ([task(root,args.id)] if args.id else sorted((root/'tasks').glob('*'))):
            if (p/'state.json').is_file(): print(json.dumps(read(p/'state.json'),ensure_ascii=False,indent=2))
        return
    with locked(root):
        p=task(root,args.id); s=read(p/'state.json'); prev=s['stage']
        if s['stage'] in ('CLOSED','ABANDONED') and args.cmd not in ('release','close'):
            raise ValueError(f'Görev kapalı ({s["stage"]}); devam için yeni görev aç.')
        if args.cmd=='close':
            if s['stage'] in CLOSED_STAGES: raise ValueError('Görev zaten kapalı: '+s['stage'])
            if s['developer']: raise ValueError('Release developer lock first.')
            stage='ABANDONED' if args.abandon else 'CLOSED'
            s['closed']={'stage':stage,'from_stage':s['stage'],'reason':args.reason,
                         'by':os.environ.get('AI_TEAM_ROLE') or 'user','at':now()}
            s['stage']=stage
            s.setdefault('automation',{}).update(enabled=False,status=stage,role=None,run_id=None,
                                                 message='Kapatıldı: '+args.reason,updated=now())
        elif args.cmd=='advance':
            try: advance(root,p,s,args.stage)
            except Blocked as e:
                s.setdefault('automation',{}).update(status='BLOCKED',role=None,run_id=None,message=str(e),updated=now())
                s['updated']=now();s['revision']+=1;atomic(p/'state.json',s)
                raise
        elif args.cmd=='rework':
            if s['developer']: raise ValueError('Release developer lock first.')
            # Every rework cycle needs its own CURRENT_STATE update; the old baseline must not satisfy it.
            s.update(stage='IMPLEMENTATION',claude_test=None,codex_verify=None,knowledge=None,
                     knowledge_baseline=knowledge_hashes(root),rework_reason=args.reason,
                     rework_count=s.get('rework_count',0)+1)
            if s['rework_count']>MAX_REWORKS:
                s.setdefault('automation',{}).update(status='BLOCKED',role=None,run_id=None,updated=now(),
                    message=f'{MAX_REWORKS} düzeltme turu aşıldı — Lead/kullanıcı kararı')
        elif args.cmd=='reclassify':
            if s['developer']: raise ValueError('Release developer lock first.')
            old=s.get('profile','full')
            if args.profile=='auto':
                args.profile,why=suggest_profile(root,p,s.get('title',''))
                print(f'Profil: {args.profile} (auto) — {why}')
            new_path=WORKFLOWS[args.profile]
            # Preserve the current checkpoint, or the nearest earlier one shared by both profiles.
            next_stage=reclassification_stage(s,new_path)
            # Approval covers one scope only; keep the audit trail but never carry a gate into the new scope.
            history=list(s.get('gate_history',[]))
            for g in s['gates']:
                history.append(dict(g,status='INVALIDATED_BY_RECLASSIFY',invalidated=now(),
                                    invalidation_reason=args.reason,previous_status=g['status']))
            # Reset the baseline so profiles that require knowledge cannot reuse the old cycle.
            s.update(profile=args.profile,stage_path=new_path,stage=next_stage,
                     claude_test=None,codex_verify=None,knowledge=None,
                     gates=[],gate_history=history,
                     knowledge_baseline=knowledge_hashes(root),
                     reclassification={'from':old,'to':args.profile,'reason':args.reason,'at':now()})
        elif args.cmd=='claim':
            if s['stage']!='IMPLEMENTATION': raise ValueError('Not implementation stage.')
            if lock_conflict(root,task_scope(p)): raise ValueError('Another developer source lock is active.')
            s['developer']={'session':args.session,'at':now()}
            atomic(claim_baseline(root,p.name),git_changes(root))
        elif args.cmd=='release':
            if not s['developer'] or s['developer']['session']!=args.session: raise ValueError('Lock belongs to another session.')
            s['developer']=None
            base=claim_baseline(root,p.name)
            if base.is_file():
                add_changed(p,s,read(base),git_changes(root)); base.unlink()
        elif args.cmd=='evidence': evidence(root,p,s,args.role,args.result,args.session,args.note,args.engine,args.model)
        elif args.cmd=='knowledge':
            if s['stage']!='KNOWLEDGE_UPDATE': raise ValueError('Wrong stage.')
            docs=knowledge_hashes(root)
            if not docs.get('CURRENT_STATE.md') or docs.get('CURRENT_STATE.md')==s['knowledge_baseline'].get('CURRENT_STATE.md'):
                raise ValueError('Update CURRENT_STATE.md with task outcome before acknowledging knowledge.')
            s['knowledge']={'summary':args.summary,'created':now(),'fingerprint':snapshot(root,scope=task_scope(p)),'documents':docs}
        elif args.cmd=='gate':
            s['gates'].append({'id':str(uuid.uuid4()),'kind':args.kind,'action':args.action,
                'target':args.target,'environment':args.environment,'status':'AWAITING_USER','created':now()})
        elif args.cmd=='resolve-gate':
            g=next((g for g in s['gates'] if g['id']==args.gate_id),None)
            if not g: raise ValueError('Gate not found.')
            if g['kind']=='DB' and args.outcome=='explicitly-approved':
                raise ValueError('DB approval never authorizes agent execution. User must apply or cancel.')
            g.update(status='RESOLVED_BY_LEAD',outcome=args.outcome,user_message_ref=args.user_message_ref,resolved=now())
        elif args.cmd=='automate':
            a=s.setdefault('automation',{})
            if args.disable:
                a.update(enabled=False,status='DISABLED',role=None,run_id=None,
                         message='Lead tarafindan durduruldu',updated=now())
            else:
                s['rework_count']=0
                a.update(enabled=True,
                         status='WAITING_FOR_LEAD' if s['stage']=='NEW' else 'IDLE',
                         role=None,run_id=None,
                         message='Otomatik akis etkin',updated=now())
        s['updated']=now();s['revision']+=1;atomic(p/'state.json',s)
        # Manual steps outside a runner role (no AI_TEAM_RUN_ID); Lead's NEW→first stage is the normal start.
        if (args.cmd in ('automate','rework','close','advance','reclassify') and not os.environ.get('AI_TEAM_RUN_ID')
                and not (args.cmd=='advance' and prev=='NEW')):
            detail={'automate':lambda:'disable' if args.disable else 'resume' if args.resume else 'enable',
                    'advance':lambda:f'{prev}→{args.stage}','reclassify':lambda:args.profile}.get(args.cmd,lambda:args.reason)()
            metric(root,{'type':'intervention','task':p.name,'cmd':args.cmd,'detail':detail,'stage':prev})
        print(json.dumps(s,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try: main()
    except (ValueError,OSError,KeyError) as e:
        print('STOP: '+str(e),file=sys.stderr);sys.exit(2)
