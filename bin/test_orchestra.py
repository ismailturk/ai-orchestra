"""AI Orchestra Personal V1: adaptive workflow, safety limits, memory, shadow context, Command Center API."""
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import team
import team_runner
import intel
import orchestra


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / 'proj'; self.root.mkdir()
        team.init_project(self.root)
        (self.root / '.ai/project.json').write_text(json.dumps({'id': 'p1', 'source_roots': [str(self.root)]}))
        env = {'XDG_CONFIG_HOME': str(self.base / 'cfg'), 'XDG_DATA_HOME': str(self.base / 'data')}
        p = patch.dict(os.environ, env); p.start(); self.addCleanup(p.stop)
        team_runner._warned.clear()

    def git(self, *a, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.root), *a], capture_output=True, text=True, check=True).stdout


class Adaptive(Base):
    def test_risk_is_judged_apart_from_size(self):
        c = lambda text, files=None: team.classify(self.root, text, files)
        self.assertEqual(c('Typo düzelt', ['lib/a.dart'])['level'], 'micro')
        self.assertEqual(c('Oturum süresi dolunca login ekranına yönlendir')['level'], 'standard')
        self.assertEqual(c('Profil ekranındaki başlığı düzelt')['profile'], 'ui')
        self.assertEqual(c('Token yenileme hatası', ['lib/a.dart'])['profile'], 'small')  # one-line auth is never micro
        self.assertEqual(c('Kullanıcı tablosuna migration ekle')['level'], 'critical')
        self.assertEqual(c('Ödeme servisini düzelt', ['lib/a.dart'])['profile'], 'risky')
        self.assertEqual(c('Bildirim modülünü refactor et')['level'], 'complex')
        self.assertEqual(c('Bir hata düzelt')['profile'], 'small')  # unknown scope: not forced into the fast path
        self.assertEqual(c('Sürüm yükselt', ['pubspec.yaml'])['profile'], 'small')  # dependency manifest

    def test_unknown_profile_and_reclassify_keep_gates(self):
        p = team.create(self.root, 'R-1', 'risky', 'risky')
        s = team.read(p / 'state.json')
        self.assertEqual(team.LEVELS[s['profile']], 'critical')
        with self.assertRaises(ValueError):
            team.create(self.root, 'R-2', 'x', 'nope')


class Safety(Base):
    def state_for(self, ident, profile='small', **kw):
        p = team.create(self.root, ident, 'x', profile, True, **kw)
        return p, team.read(p / 'state.json')

    def test_claude_roles_get_tool_layer_denies_and_no_bypass(self):
        p, s = self.state_for('S-1')
        cmd, _, _ = team_runner.command_for(self.root, 'S-1', s, 'developer', 'prompt', 'r1')
        i = cmd.index('--disallowedTools')
        denied = cmd[i + 1:]
        for rule in ('Bash(git push:*)', 'Bash(git merge:*)', 'Bash(dotnet ef database:*)', 'Bash(rm -rf:*)'):
            self.assertIn(rule, denied)
        self.assertNotIn('bypassPermissions', ' '.join(cmd)); self.assertNotIn('dangerously', ' '.join(cmd))

    def test_role_env_makes_git_push_fail_inside_git(self):
        self.git('init', '-q'); self.git('remote', 'add', 'origin', 'https://example.com/r.git')
        self.git('remote', 'add', 'gh', 'git@github.com:me/r.git')
        env = team_runner.push_block_env(dict(os.environ))
        for remote in ('origin', 'gh'):
            url = subprocess.run(['git', '-C', str(self.root), 'remote', 'get-url', '--push', remote], env=env,
                                 capture_output=True, text=True).stdout
            self.assertTrue(url.startswith('ai-orchestra-push-blocked://'), url)
        r = subprocess.run(['git', '-C', str(self.root), 'push', 'origin', 'HEAD'], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('ai-orchestra-push-blocked', r.stderr)

    def test_intake_lead_runs_at_new_and_complex_gets_opus(self):
        p, s = self.state_for('S-2', 'full', intake='Mimariyi yeniden yaz')
        self.assertEqual(team_runner.triage(self.root, 'S-2', s), 'lead')
        self.assertEqual(team_runner.role_model(self.root, s, 'lead'), 'opus')
        prompt = team_runner.workflow_prompt(self.root, 'S-2', s, 'lead', 'r1')
        self.assertIn('GÖREV GİRİŞİ', prompt); self.assertIn('Lead sorusu', prompt)
        p, s = self.state_for('S-3', 'small')  # manual task: still waits for a human Lead
        self.assertIsNone(team_runner.triage(self.root, 'S-3', s))

    def test_global_pause_starts_nothing_and_does_not_touch_running(self):
        p, s = self.state_for('S-4'); team.advance(self.root, p, s, 'IMPLEMENTATION'); team.atomic(p / 'state.json', s)
        (team.config_dir()).mkdir(parents=True, exist_ok=True); (team.config_dir() / 'paused').write_text('x')
        self.assertEqual(team_runner.dispatch_once(self.root, dry_run=True), 'PAUSED')
        self.assertEqual(team_runner.watch_loop(self.root, None, 0, (600, 600, 600), once_round=True), {})
        (team.config_dir() / 'paused').unlink()
        with patch.object(team_runner, 'command_for', return_value=(['fake'], 'claude', None)):
            self.assertEqual(team_runner.dispatch_once(self.root, dry_run=True), 'DRY_RUN')

    def run_sleeper(self, ident, setup):
        p, s = self.state_for(ident); team.advance(self.root, p, s, 'IMPLEMENTATION'); team.atomic(p / 'state.json', s)
        setup(p)
        script = self.base / 'sleeper.sh'; script.write_text('#!/bin/sh\necho "{}"\nsleep 30\n'); script.chmod(0o755)
        with patch.object(team_runner, 'command_for', return_value=([str(script)], 'claude', None)), \
                patch.object(team_runner, 'IDLE_CHECK_SECONDS', 0.2):
            t0 = time.monotonic()
            result = team_runner.dispatch_once(self.root, idle_seconds=600)
        self.assertLess(time.monotonic() - t0, 20)
        return result, team.read(p / 'state.json')

    def test_cancel_is_explicit_and_stops_only_that_role(self):
        def cancel_soon(p):
            def go():
                for _ in range(100):
                    time.sleep(0.1)
                    try:
                        if team.read(p / 'state.json')['automation'].get('status') == 'RUNNING':
                            orchestra.cancel(self.root, p.name); return
                    except (OSError, ValueError):
                        pass
            threading.Thread(target=go, daemon=True).start()
        result, s = self.run_sleeper('S-5', cancel_soon)
        self.assertEqual(result, 'BLOCKED')
        self.assertEqual(s['automation']['status'], 'BLOCKED'); self.assertIn('iptal', s['automation']['message'])
        self.assertIsNone(s['automation'].get('cancel_requested'))
        with self.assertRaises(ValueError):
            orchestra.cancel(self.root, 'S-5')  # nothing running any more

    def test_hard_role_time_limit(self):
        def limit(p):
            cfg = team.read(self.root / '.ai/runner.json'); cfg['limits'] = {'role_minutes': 0.01}
            team.atomic(self.root / '.ai/runner.json', cfg)
        result, s = self.run_sleeper('S-6', limit)
        self.assertEqual(result, 'BLOCKED')
        self.assertEqual(s['automation']['warnings'][-1]['kind'], 'ROLE_TIMEOUT')

    def test_task_time_budget_parks_for_user_until_resume(self):
        cfg = team.read(self.root / '.ai/runner.json'); cfg['limits'] = {'task_minutes': 1}
        team.atomic(self.root / '.ai/runner.json', cfg)
        p, s = self.state_for('S-7'); team.advance(self.root, p, s, 'IMPLEMENTATION'); team.atomic(p / 'state.json', s)
        team.metric(self.root, {'type': 'run', 'task': 'S-7', 'role': 'developer', 'duration_s': 90})
        self.assertIsNone(team_runner.triage(self.root, 'S-7', team.read(p / 'state.json')))
        self.assertEqual(team.read(p / 'state.json')['automation']['status'], 'AWAITING_USER')
        subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'automate', 'S-7', '--resume'],
                       check=True, capture_output=True)
        self.assertEqual(team_runner.triage(self.root, 'S-7', team.read(p / 'state.json')), 'developer')

    def test_human_gate_cannot_be_skipped(self):
        p, s = self.state_for('S-8', 'risky')
        for st in s['stage_path'][1:4]:
            team.advance(self.root, p, s, st)
        s['gates'] = [{'id': 'g', 'kind': 'DB', 'status': 'AWAITING_USER'}]; team.atomic(p / 'state.json', s)
        self.assertIsNone(team_runner.triage(self.root, 'S-8', s))
        r = subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'resolve-gate', 'S-8', 'g',
                            '--outcome', 'explicitly-approved', '--user-message-ref', 'x'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2); self.assertIn('DB approval never authorizes', r.stderr)
        team.reply(self.root, 'S-8', 'tamam onaylıyorum')  # a chat reply is never a gate approval
        self.assertEqual(team.read(p / 'state.json')['gates'][0]['status'], 'AWAITING_USER')

    def test_worktree_lock_and_admission_are_independent(self):
        self.git('init', '-q'); self.git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        a, sa = self.state_for('W-1', worktree=True)
        b, sb = self.state_for('W-2', worktree=True)
        self.assertNotEqual(sa['worktree']['src'], sb['worktree']['src'])
        self.assertTrue(self.git('branch', '--list', 'ai/W-1').strip())
        for p, s in ((a, sa), (b, sb)):
            team.advance(self.root, p, s, 'IMPLEMENTATION'); team.atomic(p / 'state.json', s)
        subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'claim', 'W-1', '--session', 's1'], check=True, capture_output=True)
        subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'claim', 'W-2', '--session', 's2'], check=True, capture_output=True)
        cfg = team_runner.runner_config(self.root)
        self.assertTrue(team_runner.admissible([('W-1', 'developer', ['\0worktree/W-1/**'])], ('W-2', 'developer', ['\0worktree/W-2/**']), cfg))
        # Evidence follows the task's own tree: a main-checkout edit does not stale it, a worktree edit does.
        src = team.task_sources(self.root, sa)
        before = team.snapshot(self.root, sources=src)
        (self.root / 'main_only.txt').write_text('x')
        self.assertEqual(before, team.snapshot(self.root, sources=src))
        (Path(sa['worktree']['src']) / 'wt.txt').write_text('x')
        self.assertNotEqual(before, team.snapshot(self.root, sources=src))
        with self.assertRaises(FileExistsError):  # same task cannot be created twice
            team.create(self.root, 'W-1', 'again', 'small', True, worktree=True)

    def test_worktree_refused_without_git(self):
        with self.assertRaises(ValueError):
            team.create(self.root, 'W-3', 'x', 'small', True, worktree=True)
        self.assertFalse((self.root / 'tasks/W-3').exists())  # no half-created task left behind


class Memory(Base):
    def setUp(self):
        super().setUp()
        self.git('init', '-q')
        files = {'lib/auth/session.dart': "import 'package:app/core/api.dart';\nclass SessionGuard { void expire() {} }\n",
                 'lib/core/api.dart': 'const apiKey = "AIzaSyA1234567890abcdefghijklmnopqrstuvw";\nclass Api {}\n',
                 'pubspec.yaml': 'name: app\ndependencies:\n  flutter:\n    sdk: flutter\n',
                 'test/session_test.dart': 'void main() {}\n', '.env': 'SECRET=topsecret123',
                 'keys/id_rsa': 'PRIVATE', 'config/credentials.json': '{"a":1}', '.claude/settings.local.json': '{"x":1}'}
        for rel, text in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True); (self.root / rel).write_text(text)

    def test_secrets_never_indexed_and_content_redacted(self):
        r = intel.index(self.root)
        self.assertGreaterEqual(r['skipped_secret_or_tool'], 4)
        c = intel.db(self.root)
        paths = {p for (p,) in c.execute('select path from files')}
        body = ' '.join(b for (b,) in c.execute('select body from fts'))
        c.close()
        for secret in ('.env', 'keys/id_rsa', 'config/credentials.json', '.claude/settings.local.json'):
            self.assertNotIn(secret, paths)
        self.assertNotIn('topsecret123', body); self.assertNotIn('AIzaSyA1234567890', body)
        self.assertIn('lib/auth/session.dart', paths)
        self.assertEqual(intel.search(self.root, 'SessionGuard')[0]['path'], 'lib/auth/session.dart')

    def test_incremental_index_marks_stale_facts_and_keeps_kinds_apart(self):
        intel.index(self.root)
        intel.add_fact(self.root, 'user_decision', 'Oturum süresi 30 dk', 'lib/auth/session.dart')
        intel.add_fact(self.root, 'inference', 'Api tekil olabilir', 'lib/core/api.dart')
        with self.assertRaises(ValueError):
            intel.add_fact(self.root, 'verified', 'ajan doğruladığını iddia ediyor')
        self.assertEqual(intel.index(self.root)['changed'], 0)
        (self.root / 'lib/auth/session.dart').write_text('class SessionGuard2 {}\n')
        self.assertEqual(intel.index(self.root)['changed'], 1)
        facts = {f['text']: f for f in intel.summary(self.root)['facts']}
        self.assertEqual(facts['Oturum süresi 30 dk']['status'], 'stale')
        self.assertEqual(facts['Api tekil olabilir']['status'], 'current')
        self.assertEqual(facts['Api tekil olabilir']['kind'], 'inference')
        self.assertTrue(any(f['kind'] == 'verified' and 'Flutter' in f['text'] for f in facts.values()))
        (self.root / 'lib/core/api.dart').unlink(); intel.index(self.root)
        self.assertNotIn('lib/core/api.dart', [r['path'] for r in intel.search(self.root, 'apiKey')])

    def test_skills_at_most_three_and_installed_not_duplicated(self):
        intel.index(self.root)
        s = intel.suggest_skills(self.root, 'login oturum token hatası ekran widget testi crash')
        self.assertLessEqual(len(s), 3)
        self.assertEqual(s[0]['id'], 'security-review')
        (self.root / '.claude/skills/security-review').mkdir(parents=True)
        s = intel.suggest_skills(self.root, 'login oturum token hatası')
        self.assertTrue(s[0]['installed']); self.assertIn('zaten yüklü', s[0]['use'])
        self.assertEqual(intel.suggest_skills(self.root, 'tamamen alakasız istek'), [])

    def test_shadow_mode_never_changes_prompt_or_command(self):
        p = team.create(self.root, 'C-1', 'Session expire', 'small', True)
        s = team.read(p / 'state.json'); team.advance(self.root, p, s, 'IMPLEMENTATION'); team.atomic(p / 'state.json', s)
        s = team.read(p / 'state.json')
        prompt = team_runner.workflow_prompt(self.root, 'C-1', s, 'developer', 'r1')
        fixed = __import__('uuid').UUID('12345678-1234-5678-1234-567812345678')
        sid = patch.object(team_runner.uuid, 'uuid4', return_value=fixed); sid.start(); self.addCleanup(sid.stop)
        cmd = team_runner.command_for(self.root, 'C-1', s, 'developer', prompt, 'r1')
        rec = team.read(intel.shadow_context(self.root, 'C-1', 'developer', prompt, 'r1'))  # no index: safe default
        self.assertEqual(rec['proposed']['relevant_files'], [])
        self.assertTrue(any('indeksi yok' in n for n in rec['notes']))
        intel.index(self.root)
        rec = team.read(intel.shadow_context(self.root, 'C-1', 'developer', prompt, 'r2'))
        self.assertEqual(prompt, team_runner.workflow_prompt(self.root, 'C-1', s, 'developer', 'r1'))
        self.assertEqual(cmd, team_runner.command_for(self.root, 'C-1', s, 'developer', prompt, 'r1'))
        self.assertEqual(rec['prompt_sha256'], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertIn('lib/auth/session.dart', rec['proposed']['relevant_files'])
        for keep in ('.ai/TEAM_RULES.md', 'roles/developer.md'):  # quality/safety sources are never dropped
            self.assertIn(keep, rec['proposed']['mandatory_reads'])
        # Through the runner: the launched command is identical with shadow mode on and off.
        seen = []
        fake = lambda root, ident, role, command, *a, **k: seen.append(command) or (1, None, self.base / 'none.log')
        for shadow in (True, False):
            cfg = team.read(self.root / '.ai/runner.json'); cfg['context_shadow'] = shadow
            team.atomic(self.root / '.ai/runner.json', cfg)
            with team.locked(self.root):
                st = team.read(p / 'state.json'); st['automation'].update(status='IDLE'); st['sessions'] = {}; team.atomic(p / 'state.json', st)
            with patch.object(team_runner, 'run_process', side_effect=fake), \
                    patch.object(team_runner.uuid, 'uuid4', return_value=fixed), \
                    patch.object(team_runner, 'snapshot_task_markdown', return_value=self.base):
                team_runner.dispatch_once(self.root)
        self.assertEqual(len(seen), 2); self.assertEqual(seen[0], seen[1])
        self.assertEqual(len(list((self.root / '.ai/context-shadow/C-1').glob('developer-*.json'))), 3)


class Notifications(Base):
    def test_telegram_is_opt_in_private_outbound_and_redacted(self):
        sent = []
        with patch('urllib.request.urlopen', side_effect=lambda url, data, timeout: sent.append((url, data)) or __import__('io').BytesIO()):
            self.assertIsNone(team_runner.send_telegram('AI Team tamamlandı', 'x'))  # no config: nothing
            f = team.config_dir() / 'telegram.json'; f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({'enabled': True, 'bot_token': '123:ABC', 'chat_id': '42'})); f.chmod(0o644)
            self.assertIsNone(team_runner.send_telegram('AI Team tamamlandı', 'x'))  # world-readable token: refused
            f.chmod(0o600)
            self.assertIsNone(team_runner.send_telegram('AI Team · T1 · tester', 'heartbeat'))  # heartbeats stay local
            team_runner.send_telegram('AI Team rol hatası', 'T1: password=hunter2hunter').join(5)
        self.assertEqual(len(sent), 1)
        self.assertIn('/bot123:ABC/sendMessage', sent[0][0])
        body = __import__('urllib.parse').parse.parse_qs(sent[0][1].decode())['text'][0]
        self.assertIn('AI Team rol hatası', body); self.assertNotIn('hunter2hunter', body)


class Graph(Base):
    def test_dependents_follow_imports_transitively(self):
        files = {'pubspec.yaml': 'name: app\n', 'lib/core/api.dart': 'class Api {}\n',
                 'lib/auth/session.dart': "import 'package:app/core/api.dart';\n",
                 'lib/ui/login.dart': "import '../auth/session.dart';\n", 'lib/other.dart': "import 'dart:io';\n"}
        self.git('init', '-q')
        for rel, text in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True); (self.root / rel).write_text(text)
        intel.index(self.root)
        self.assertEqual(intel.dependents(self.root, 'lib/core/api.dart'), ['lib/auth/session.dart', 'lib/ui/login.dart'])
        self.assertEqual(intel.dependents(self.root, 'lib/core/api.dart', depth=1), ['lib/auth/session.dart'])
        self.assertEqual(intel.dependents(self.root, 'lib/other.dart'), [])


class Api(Base):
    def setUp(self):
        super().setUp()
        team.atomic(team.config_dir() / 'projects.json', {'p1': {'root': str(self.root)}})
        self.srv = orchestra.make_server(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)

    def call(self, method, path, body=None, token=True, host=None, origin=None, ctype='application/json'):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': host or f'127.0.0.1:{self.port}'}
        if token:
            h['X-Orchestra-Token'] = self.srv.token if token is True else token
        if origin:
            h['Origin'] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode(); h['Content-Type'] = ctype
        c.request(method, path, data, h)
        r = c.getresponse(); raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode()

    def test_unauthorized_and_cross_site_requests_are_rejected(self):
        self.assertEqual(self.call('GET', '/api/projects', token=False)[0], 401)
        self.assertEqual(self.call('GET', '/api/projects', token='wrong')[0], 401)
        self.assertEqual(self.call('GET', '/api/projects', host='evil.com')[0], 403)  # DNS rebinding
        self.assertEqual(self.call('GET', '/', host=f'evil.com:{self.port}')[0], 403)
        body = {'project': 'p1', 'text': 'Profil ekranındaki yazıyı düzelt', 'start_runner': False}
        self.assertEqual(self.call('POST', '/api/tasks', body, origin='https://evil.com')[0], 403)
        self.assertEqual(self.call('POST', '/api/tasks', body, ctype='text/plain')[0], 415)
        self.assertEqual(self.call('POST', '/api/tasks', body, token=False)[0], 401)
        self.assertEqual(list((self.root / 'tasks').glob('*')), [])  # nothing was created
        self.assertEqual(self.call('POST', '/api/tasks', {**body, 'project': '../etc'})[0], 400)
        self.assertEqual(oct((team.config_dir() / 'token').stat().st_mode & 0o777), '0o600')

    def test_dashboard_task_is_real_state_and_reads_do_not_write(self):
        st, page = self.call('GET', '/')
        self.assertEqual(st, 200); self.assertIn(self.srv.token, page)
        st, r = self.call('POST', '/api/tasks', {'project': 'p1', 'text': 'Oturum süresi dolunca login ekranına yönlendir',
                                                 'start_runner': False}, origin=f'http://127.0.0.1:{self.port}')
        self.assertEqual(st, 200, r)
        s = team.read(self.root / 'tasks' / r['id'] / 'state.json')
        self.assertEqual((s['stage'], s['automation']['status'], s['profile']), ('NEW', 'WAITING_FOR_LEAD', 'small'))
        self.assertEqual(r['classification']['level'], 'standard')
        self.assertEqual(s['intake']['text'], 'Oturum süresi dolunca login ekranına yönlendir')
        self.assertEqual(team_runner.triage(self.root, r['id'], s), 'lead')
        before = {f: f.stat().st_mtime_ns for f in self.root.rglob('*') if f.is_file()}
        for path in ('/api/projects', '/api/tasks?project=p1', '/api/skills?project=p1', '/api/dependents?project=p1&path=x', f'/api/task?project=p1&id={r["id"]}', '/api/activity?project=p1',
                     '/api/tokens?project=p1', '/api/settings?project=p1', '/api/summary', '/api/memory?project=p1'):
            st, j = self.call('GET', path)
            self.assertEqual(st, 200, (path, j))
        self.assertEqual(before, {f: f.stat().st_mtime_ns for f in self.root.rglob('*') if f.is_file()})
        self.assertEqual(self.call('GET', '/api/summary')[1]['active_tasks'], 1)
        st, j = self.call('POST', '/api/reply', {'project': 'p1', 'id': r['id'], 'text': 'Ayarlar ekranı'})
        self.assertEqual(st, 200)
        self.assertEqual(self.call('POST', '/api/cancel', {'project': 'p1', 'id': r['id']})[0], 400)  # needs confirm

    def test_logs_shown_masked_and_tokens_labelled_api_equivalent(self):
        log = orchestra.runner_log(self.root); log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('role › export ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz\npassword: hunter2hunter\n')
        lines = ' '.join(self.call('GET', '/api/activity?project=p1')[1]['runner_log'])
        self.assertNotIn('sk-ant-api03', lines); self.assertNotIn('hunter2hunter', lines); self.assertIn('gizlendi', lines)
        team.metric(self.root, {'type': 'run', 'task': 'X', 'role': 'tester', 'model': 'sonnet', 'result': 'limit',
                                'tokens': {'input': 10, 'output': 5}, 'cost_usd': 0.5, 'duration_s': 60})
        j = self.call('GET', '/api/tokens?project=p1')[1]
        self.assertEqual((j['totals']['limit_hits'], j['totals']['api_eq_usd']), (1, 0.5))
        self.assertIn('gerçek fatura değildir', j['note'])

    def test_pause_resume_and_project_registration(self):
        self.assertTrue(self.call('POST', '/api/pause', {})[1]['paused']); self.assertTrue(team.paused())
        self.assertFalse(self.call('POST', '/api/resume', {})[1]['paused']); self.assertFalse(team.paused())
        other = self.base / 'other'; other.mkdir()
        st, j = self.call('POST', '/api/projects', {'name': 'other', 'root': str(other), 'init': True})
        self.assertEqual(st, 200, j); self.assertTrue((other / '.ai/PROJECT.md').is_file())
        self.assertEqual(self.call('POST', '/api/projects', {'name': 'other', 'root': str(other)})[0], 400)
        self.assertIn('other', [p['name'] for p in self.call('GET', '/api/projects')[1]['projects']])


class Tui(unittest.TestCase):
    def test_board_lists_user_waits_first_then_running_then_recent_closed(self):
        import tui
        row = lambda i, stage, status, gates=(): {'id': i, 'stage': stage, 'status': status, 'gates': list(gates)}
        rows = [row('Q', 'IMPLEMENTATION', 'IDLE'), row('R', 'TEST_CLAUDE', 'RUNNING'), row('B', 'VERIFY_CODEX', 'BLOCKED'),
                row('G', 'IMPLEMENTATION', 'IDLE', [{'kind': 'DB'}]), row('X', 'CLOSED', 'CLOSED', [{'kind': 'DB'}])] + \
               [row(f'D{n}', 'DONE', 'COMPLETED') for n in range(7)]
        self.assertEqual([t['id'] for t in tui.visible(rows)], ['B', 'G', 'R', 'Q', 'X', 'D0', 'D1', 'D2', 'D3'])
        self.assertFalse(tui.needs_user(rows[4]))  # a closed task's leftover gate does not wait on the user


if __name__ == '__main__':
    unittest.main(verbosity=2)
