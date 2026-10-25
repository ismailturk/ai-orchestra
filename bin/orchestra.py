#!/usr/bin/env python3
"""AI Orchestra Command Center: local dashboard + JSON API over team.py / team_runner.py.

Binds 127.0.0.1 only. Every /api call needs the per-machine token (~/.config/ai-team/token); the Host
header must be this server (DNS rebinding) and a POST must be same-origin JSON (CSRF). No second state
machine: every write goes through team.py functions or its CLI.
"""
import argparse
import datetime as dt
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import team
import team_runner
import intel

DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024


def token():
    f = team.config_dir() / 'token'
    if not f.is_file():
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as h:
            h.write(secrets.token_urlsafe(32))
    return f.read_text().strip()


def registry():
    """{name: root path}; only registered projects are reachable through the API (no arbitrary paths)."""
    out = {}
    for name, v in team.load_projects().items():
        r = v.get('root') if isinstance(v, dict) else v
        if r:
            out[name] = Path(r).expanduser()
    return out


def root_of(name):
    r = registry().get(name)
    if not r:
        raise ValueError('Bilinmeyen proje: ' + str(name))
    return team.project(r)


def runner_online(root):
    try:
        pid = int((root / '.ai' / '.runner.pid').read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def runner_log(root):
    return root / '.ai' / 'runner-logs' / '_team_runner.log'


def start_runner(root):
    if runner_online(root):
        return 'zaten çalışıyor'
    log = runner_log(root); log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a') as h:
        subprocess.Popen([sys.executable, str(BIN / 'team_runner.py'), '--project', str(root), 'watch'],
                         cwd=root, stdout=h, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return 'başlatıldı'


def tail(path, size=48_000):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END); f.seek(max(0, f.tell() - size))
            return intel.redact(f.read().decode(errors='replace'))
    except OSError:
        return ''


def task_rows(root):
    rows = []
    for f in sorted((root / 'tasks').glob('*/state.json')):
        ident = f.parent.name
        try:
            s = team.read(f)
        except (OSError, ValueError) as e:
            rows.append({'id': ident, 'stage': '?', 'status': 'STATE_ERROR', 'message': f'state okunamadı: {e}'})
            continue
        a = s.get('automation') or {}
        gates = [g for g in s.get('gates') or [] if g.get('status') != 'RESOLVED_BY_LEAD']
        rows.append({'id': ident, 'title': s.get('title'), 'profile': s.get('profile'),
                     'level': team.LEVELS.get(s.get('profile')), 'stage': s.get('stage'), 'path': s.get('stage_path'),
                     'status': a.get('status') if a.get('enabled') or a.get('status') in team.CLOSED_STAGES else (a.get('status') or 'MANUAL'),
                     'enabled': bool(a.get('enabled')), 'role': a.get('role'), 'run_id': a.get('run_id'),
                     'message': a.get('message'), 'retry_at': a.get('retry_at'), 'rework': s.get('rework_count', 0), 'gates': gates,
                     'created': s.get('created'), 'updated': s.get('updated'), 'intake': bool(s.get('intake')),
                     'worktree': (s.get('worktree') or {}).get('branch'), 'escalated': s.get('escalated'),
                     'label': team.pass_label(s) if s.get('stage') == 'DONE' else None,
                     'changed': len(s.get('changed_files') or []), 'commit': s.get('commit')})
    return sorted(rows, key=lambda r: r.get('updated') or '', reverse=True)


def render_log(path, limit=120):
    """Role stream-json log as short readable lines (same parsing as the runner panel)."""
    out = []
    for line in tail(path, 200_000).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        k, item = e.get('type'), e.get('item') or {}
        if k == 'assistant':
            for b in (e.get('message') or {}).get('content') or []:
                if b.get('type') == 'text' and (b.get('text') or '').strip():
                    out.append('› ' + b['text'].strip()[:400])
                elif b.get('type') == 'tool_use':
                    out.append('$ ' + team_runner.tool_summary(b)[:300])
        elif k == 'item.completed' and item.get('type') == 'agent_message':
            out.append('› ' + (item.get('text') or '').strip()[:400])
        elif k == 'item.started' and item.get('type') == 'command_execution':
            out.append('$ ' + (item.get('command') or '').strip().splitlines()[0][:300] if item.get('command') else '$ ?')
        elif k == 'result':
            out.append(f'· sonuç: {e.get("subtype")} · {e.get("num_turns") or "?"} tur')
        elif k in ('error', 'turn.failed'):
            out.append('! ' + json.dumps(e, ensure_ascii=False)[:300])
    return out[-limit:]


DETAIL_DOCS = ('TASK', 'ANALYSIS', 'DESIGN', 'IMPLEMENTATION', 'TEST_CLAUDE', 'VERIFY_CODEX', 'FINAL_REPORT')


def task_detail(root, ident):
    p = team.task(root, ident)
    s = team.read(p / 'state.json')
    s.pop('sessions', None)  # agent session ids are not shown or copied
    docs = {}
    for d in DETAIL_DOCS:
        f = p / (d + '.md')
        if f.is_file():
            text = f.read_text(errors='replace')
            if 'STATUS: NOT_RUN' not in text or d == 'TASK':
                docs[d] = intel.redact(text[:20_000])
    msgs = []
    for f in sorted((root / '.ai' / 'messages').glob('*/*.json')):
        try:
            m = team.read(f)
        except (OSError, ValueError):
            continue
        if m.get('task') == ident:
            msgs.append({k: m.get(k) for k in ('from', 'to', 'created')} | {'body': intel.redact(str(m.get('body'))[:2000])})
    logs = sorted((root / '.ai' / 'runner-logs' / ident).glob('*.log'), key=lambda f: f.stat().st_mtime)
    return {'state': s, 'docs': docs, 'messages': sorted(msgs, key=lambda m: m.get('created') or '')[-30:],
            'question': team_runner.lead_question(root, ident),
            'log': {'file': logs[-1].name, 'lines': render_log(logs[-1])} if logs else None,
            'shadow': intel.shadow_records(root, ident)[-10:],
            'runs': [{'role': r.get('role'), 'model': r.get('model'), 'engine': r.get('engine'), 'result': r.get('result'),
                      'start': r.get('ts_start') or r.get('ts'), 'seconds': r.get('duration_s'), 'usd': r.get('cost_usd'),
                      'tokens': sum((r.get('tokens') or {}).get(k) or 0 for k in team.TOKEN_KEYS)}
                     for r in team.load_metrics(root) if r.get('type') == 'run' and r.get('task') == ident][-40:],
            # A command, not team.commit(): even its dry run may record state, and a GET must not write.
            'commit_hint': f'team --project {root} commit {ident}' if s.get('stage') == 'DONE' else None}


def tokens(root):
    rows = team.load_metrics(root)
    runs = [r for r in rows if r.get('type') == 'run']
    states = {r['id']: r for r in task_rows(root)}
    per_task, per_rm = {}, {}
    for r in runs:
        t = per_task.setdefault(r.get('task') or '?', {'runs': 0, 'input': 0, 'output': 0, 'cache_read': 0,
                                                        'cache_creation': 0, 'api_eq_usd': 0.0, 'seconds': 0, 'limit_hits': 0})
        k = per_rm.setdefault(f"{r.get('role') or '?'} · {r.get('model') or '-'}",
                              {'runs': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cache_creation': 0, 'api_eq_usd': 0.0})
        for a in (t, k):
            a['runs'] += 1
            for key in team.TOKEN_KEYS:
                a[key] += (r.get('tokens') or {}).get(key) or 0
            a['api_eq_usd'] += r.get('cost_usd') or 0
        t['seconds'] += r.get('duration_s') or 0
        t['limit_hits'] += r.get('result') == 'limit'
    for ident, t in per_task.items():
        s = states.get(ident) or {}
        t.update(stage=s.get('stage'), rework=s.get('rework'), status=s.get('status'),
                 first_try=s.get('stage') == 'DONE' and not s.get('rework'))
    done = [t for t in per_task.values() if t['stage'] == 'DONE']
    total = lambda key: sum(t[key] for t in per_task.values())
    # Quality-adjusted view: what a finished task costs per profile, and how often it passed first time.
    per_profile, daily = {}, {}
    for ident, s in states.items():
        t = per_task.get(ident)
        if not t:
            continue
        g = per_profile.setdefault(s.get('profile') or '?', {'tasks': 0, 'done': 0, 'first_try': 0, 'tokens': 0, 'api_eq_usd': 0.0, 'reworks': 0})
        g['tasks'] += 1; g['done'] += t['stage'] == 'DONE'; g['first_try'] += t['first_try']
        g['tokens'] += sum(t[k] for k in team.TOKEN_KEYS); g['api_eq_usd'] += t['api_eq_usd']; g['reworks'] += s.get('rework') or 0
    for r in runs:
        d = daily.setdefault((r.get('ts_start') or r.get('ts') or '')[:10], {'runs': 0, 'tokens': 0, 'api_eq_usd': 0.0})
        d['runs'] += 1; d['tokens'] += sum((r.get('tokens') or {}).get(k) or 0 for k in team.TOKEN_KEYS)
        d['api_eq_usd'] += r.get('cost_usd') or 0
    shadow = intel.shadow_records(root)
    return {'tasks': per_task, 'role_model': per_rm, 'profiles': per_profile, 'daily': dict(sorted(daily.items())[-30:]),
            'shadow': {'records': len(shadow), 'orig_tokens_est': sum(r.get('original_tokens_est') or 0 for r in shadow),
                       'saving_tokens_est': sum(r.get('saving_tokens_est') or 0 for r in shadow)},
            'totals': {'runs': len(runs), 'done': len(done), 'first_try': sum(t['first_try'] for t in done),
                       'failed': sum(1 for s in states.values() if s.get('status') == 'BLOCKED' or s.get('stage') == 'ABANDONED'),
                       'limit_hits': total('limit_hits'), 'api_eq_usd': round(total('api_eq_usd'), 4),
                       'tokens': sum(total(k) for k in team.TOKEN_KEYS),
                       'tokens_per_done': round(sum(total(k) for k in team.TOKEN_KEYS) / len(done)) if done else None},
            'note': 'API eşdeğeri tahmindir; Claude aboneliğinde gerçek fatura değildir. Codex satırlarında maliyet yok.'}


def project_info(name, r):
    info = {'name': name, 'root': str(r), 'onboarded': (r / '.ai/PROJECT.md').is_file()}
    if info['onboarded']:
        info['runner'] = runner_online(r)
        info['memory'] = intel.summary(r)
        info['git'] = team.git(r, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() or None
    return info


def summary():
    """Compact view for the menu bar."""
    active, agents, approvals, last_error, online, latest = 0, [], 0, None, [], ('', None)
    for name, r in registry().items():
        if not (r / '.ai/PROJECT.md').is_file():
            continue
        if runner_online(r):
            online.append(name)
        for t in task_rows(r):
            if t['stage'] in team.CLOSED_STAGES:
                continue
            active += 1
            if t['status'] == 'RUNNING':
                agents.append(f"{name}/{t['id']}: {t['role']}")
            approvals += len(t['gates']) + (t['status'] == 'BLOCKED' and (t.get('message') or '').startswith('Lead sorusu'))
            if t['status'] in ('BLOCKED', 'STATE_ERROR') and (t.get('updated') or '') > (last_error or ('',))[0]:
                last_error = (t.get('updated') or '', f"{name}/{t['id']}: {t.get('message') or t['status']}")
            if (t.get('updated') or '') > latest[0]:
                latest = (t.get('updated') or '', name)
    return {'online': online, 'active_project': latest[1], 'active_tasks': active, 'agents': agents,
            'approvals': approvals, 'last_error': last_error[1] if last_error else None, 'paused': team.paused()}


def new_task_id(root):
    base = 'T' + dt.datetime.now().strftime('%m%d-%H%M')
    for i in range(100):
        ident = base + (f'-{i}' if i else '')
        if not (root / 'tasks' / ident).exists():
            return ident
    raise ValueError('Görev kimliği üretilemedi')


def submit_task(root, text, worktree=False, start=True):
    text = (text or '').strip()
    if len(text) < 8:
        raise ValueError('Görev metni çok kısa.')
    c = team.classify(root, text)
    skills = intel.suggest_skills(root, text)
    ident = new_task_id(root)
    title = ' '.join(text.split())[:80]
    p = team.create(root, ident, title, c['profile'], True, None, text, worktree)
    with team.locked(root):
        s = team.read(p / 'state.json')
        s.update(classification=c, skills=skills, updated=team.now(), revision=s.get('revision', 0) + 1)
        s['automation'].update(status='WAITING_FOR_LEAD', message='Lead görev analizini yapacak', updated=team.now())
        team.atomic(p / 'state.json', s)
    team.metric(root, {'type': 'intake', 'task': ident, 'profile': c['profile'], 'level': c['level'],
                       'skills': [x['id'] for x in skills], 'worktree': bool(worktree)})
    runner = start_runner(root) if start else ('çalışıyor' if runner_online(root) else 'kapalı')
    return {'id': ident, 'classification': c, 'skills': skills, 'runner': runner,
            'worktree': (team.read(p / 'state.json').get('worktree') or {}).get('branch')}


def cli(root, *args):
    r = subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(root), *args], capture_output=True, text=True)
    if r.returncode:
        raise ValueError((r.stderr or r.stdout).strip().removeprefix('STOP: '))
    return r.stdout.strip()


def cancel(root, ident):
    with team.locked(root):
        p = team.task(root, ident); s = team.read(p / 'state.json'); a = s.setdefault('automation', {})
        if a.get('status') != 'RUNNING' or not a.get('run_id'):
            raise ValueError('Görevde çalışan rol yok.')
        a.update(cancel_requested=a['run_id'], message='İptal istendi; runner rolü durduracak', updated=team.now())
        s['updated'] = team.now(); s['revision'] = s.get('revision', 0) + 1
        team.atomic(p / 'state.json', s)
    team.metric(root, {'type': 'intervention', 'task': ident, 'cmd': 'cancel', 'detail': a['run_id']})
    return 'iptal istendi'


def add_project(name, path, init=False):
    team.slug(name)
    r = Path(path).expanduser().resolve()
    if not r.is_dir():
        raise ValueError('Klasör yok: ' + str(r))
    reg = team.load_projects()
    if name in reg:
        raise ValueError('Bu adla proje zaten kayıtlı.')
    reg[name] = {'root': str(r)}
    f = team.config_dir() / 'projects.json'
    team.atomic(f, reg)
    return team.init_project(r) if init else 'kaydedildi (team init yapılmadı)'


def post(path, b):
    if path == '/api/pause':
        f = team.config_dir() / 'paused'; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(team.now())
        return {'paused': True}
    if path == '/api/resume':
        (team.config_dir() / 'paused').unlink(missing_ok=True)
        return {'paused': False}
    if path == '/api/projects':
        return {'result': add_project(b.get('name', ''), b.get('root', ''), bool(b.get('init')))}
    root = root_of(b.get('project'))
    if path == '/api/tasks':
        return submit_task(root, b.get('text'), bool(b.get('worktree')), b.get('start_runner', True))
    if path == '/api/runner':
        return {'runner': start_runner(root)}
    if path == '/api/index':
        return intel.index(root)
    if path == '/api/facts':
        intel.add_fact(root, 'user_decision', str(b.get('text') or ''), b.get('source') or None)
        return {'ok': True}
    ident = team.slug(str(b.get('id') or ''))
    if path == '/api/reply':
        team.reply(root, ident, b.get('text')); return {'ok': True}
    if path == '/api/cancel':
        if b.get('confirm') != 'IPTAL':
            raise ValueError('İptal için confirm: "IPTAL" gerekir.')
        return {'result': cancel(root, ident)}
    if path == '/api/automate':
        act = {'resume': '--resume', 'disable': '--disable'}.get(b.get('action'))
        if not act:
            raise ValueError('action: resume | disable')
        cli(root, 'automate', ident, act); return {'ok': True}
    if path == '/api/gate':
        if b.get('confirm') != 'ONAY':
            raise ValueError('Gate kararı için confirm: "ONAY" gerekir.')
        out = cli(root, 'resolve-gate', ident, str(b.get('gate_id')), '--outcome', str(b.get('outcome')),
                  '--user-message-ref', 'command-center:' + team.now())
        return {'ok': True, 'detail': out[-200:]}
    raise LookupError(path)


def get(path, q):
    one = lambda k: (q.get(k) or [None])[0]
    if path == '/api/summary':
        return summary()
    if path == '/api/projects':
        return {'projects': [project_info(n, r) for n, r in registry().items()], 'paused': team.paused()}
    root = root_of(one('project'))
    if path == '/api/tasks':
        return {'tasks': task_rows(root), 'runner': runner_online(root), 'paused': team.paused()}
    if path == '/api/task':
        return task_detail(root, team.slug(one('id') or ''))
    if path == '/api/activity':
        return {'runner_log': tail(runner_log(root)).splitlines()[-300:],
                'events': [r for r in team.load_metrics(root) if r.get('type') in ('intervention', 'intake')][-50:]}
    if path == '/api/tokens':
        return tokens(root)
    if path == '/api/settings':
        return {'runner': team_runner.runner_config(root), 'models': team.read(root / '.ai/models.json')
                if (root / '.ai/models.json').is_file() else {}, 'limits': team.limits(root),
                'budget': team.budget_state(root)['cfg'], 'paused': team.paused(), 'config_dir': str(team.config_dir()),
                'worktrees': str(team.data_dir() / 'worktrees')}
    if path == '/api/search':
        return {'results': intel.search(root, one('q') or '')}
    if path == '/api/memory':
        return intel.summary(root)
    if path == '/api/skills':
        used = {}
        for r in team.load_metrics(root):
            for sid in (r.get('skills') or []) if r.get('type') == 'intake' else []:
                used[sid] = used.get(sid, 0) + 1
        return {'skills': [{**{k: s.get(k) for k in ('id', 'name', 'description', 'keywords', 'tech')},
                            'installed': intel.installed_skill(root, s['id']), 'suggested': used.get(s['id'], 0)}
                           for s in intel.skill_catalog()],
                'note': 'Yalnız templates/skills.json içinde "approved": true olan yerel açıklamalar kullanılır; internetten indirme yok.'}
    if path == '/api/dependents':
        return {'path': one('path'), 'dependents': intel.dependents(root, one('path') or '')}
    raise LookupError(path)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'AIOrchestra'
    sys_version = ''

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        for k, v in (('Content-Type', ctype), ('Content-Length', str(len(data))), ('Cache-Control', 'no-store'),
                     ('X-Frame-Options', 'DENY'), ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                     ('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                                 "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")):
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)

    def allowed_host(self):
        port = self.server.server_address[1]
        return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

    def authorized(self):
        return hmac.compare_digest(self.headers.get('X-Orchestra-Token') or '', self.server.token)

    def guard(self):
        if not self.allowed_host():
            self.send(403, {'error': 'Host reddedildi'}); return False
        if urlparse(self.path).path.startswith('/api/') and not self.authorized():
            self.send(401, {'error': 'Yetkisiz'}); return False
        return True

    def run(self, fn):
        try:
            self.send(200, fn())
        except LookupError:
            self.send(404, {'error': 'Bulunamadı'})
        except (ValueError, OSError, FileExistsError) as e:
            self.send(400, {'error': str(e)})

    def do_GET(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        if u.path == '/':
            html = (team.ROOT / 'templates' / 'command_center.html').read_text().replace('__TOKEN__', self.server.token)
            return self.send(200, html.encode(), 'text/html; charset=utf-8')
        self.run(lambda: get(u.path, parse_qs(u.query)))

    def do_POST(self):
        if not self.guard():
            return
        port = self.server.server_address[1]
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            return self.send(403, {'error': 'Origin reddedildi'})
        if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            return self.send(415, {'error': 'JSON gerekli'})
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            return self.send(413, {'error': 'Gövde çok büyük'})
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {'error': 'Geçersiz JSON'})
        self.run(lambda: post(urlparse(self.path).path, body))

    def log_message(self, fmt, *args):
        pass  # no request log: URLs/bodies may carry task text


def make_server(port=DEFAULT_PORT):
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    srv.token = token()
    return srv


def main():
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('--port', type=int, default=DEFAULT_PORT)
    a.add_argument('--open', action='store_true')
    args = a.parse_args()
    srv = make_server(args.port)
    port = srv.server_address[1]
    team.atomic(team.config_dir() / 'server.json', {'port': port, 'pid': os.getpid(), 'started': team.now()})
    url = f'http://127.0.0.1:{port}/'
    print('AI Orchestra Command Center: ' + url + ' (Ctrl-C ile durdur)', flush=True)
    if args.open:
        import webbrowser; webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        (team.config_dir() / 'server.json').unlink(missing_ok=True)


if __name__ == '__main__':
    main()
