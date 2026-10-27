import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import team
import team_runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'; self.root.mkdir()
        (self.root/'.ai').mkdir()
        (self.root/'.ai/PROJECT.md').write_text('fixture')
        (self.root/'.ai/CURRENT_STATE.md').write_text('initial')
        (self.root/'.ai/project.json').write_text(json.dumps({'id':'fixture','source_roots':[str(self.root)]}))
        (self.root/'roles').mkdir()
        for role in team.ROLES: (self.root/'roles'/f'{role}.md').write_text(role)
        self.templates = Path(self.tmp.name)/'central/templates/task'; self.templates.mkdir(parents=True)
        for doc in team.DOCS:
            (self.templates/f'{doc}.md').write_text('{{TASK_ID}} {{TITLE}}\nSTATUS: NOT_RUN\n'+'x'*140)
        self.root_patch = patch.object(team, 'ROOT', self.templates.parent.parent)
        iso = patch.dict('os.environ', {'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'cfg'),
                                        'CODEX_HOME': str(Path(self.tmp.name) / 'codex')})
        iso.start(); self.addCleanup(iso.stop)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)


    def test_dotnet_root_is_the_dotnet_on_path(self):
        bindir = self.root / 'sdk'; bindir.mkdir()
        (bindir / 'dotnet').write_text('#!/bin/sh\n'); (bindir / 'dotnet').chmod(0o755)
        with patch.dict(team_runner.os.environ, {'PATH': f'{bindir}:/usr/bin'}):
            self.assertEqual(team_runner.dotnet_root(), str(bindir.resolve()))
        with patch.dict(team_runner.os.environ, {'PATH': '/nonexistent'}):
            self.assertIsNone(team_runner.dotnet_root())
    def test_verifier_preflight_probes_the_toolchain_in_its_own_sandbox(self):
        cfg = {}
        self.assertIsNone(team_runner.sandbox_probe(self.root, {}))  # no Flutter/.NET project: nothing to probe
        (self.root / 'pubspec.yaml').write_text('name: x')
        team_runner._preflight.clear()
        with patch.object(team_runner, 'executable', return_value='/usr/bin/false'):
            label, argv, cwd = team_runner.sandbox_probe(self.root, {})
            self.assertEqual((argv[1], argv[-2:], cwd), ('sandbox', ['flutter', '--version'], self.root))
            self.assertIn('sandbox_mode="workspace-write"', argv)
            self.assertIn("Codex sandbox'ında `flutter --version` başarısız", team_runner.preflight(self.root, 'verifier', cfg, {}))
            self.assertIsNone(team_runner.preflight(self.root, 'tester', cfg, {}))  # verifier only
        team_runner._preflight.clear()
        with patch.object(team_runner, 'executable', return_value='/usr/bin/true'):
            self.assertIsNone(team_runner.preflight(self.root, 'verifier', cfg, {}))
        team_runner._preflight.clear()

    def test_legacy_and_disabled_tasks_are_never_dispatched(self):
        team.create(self.root, 'OFF-001', 'off')
        self.assertEqual(team_runner.dispatch_once(self.root, dry_run=True), 'IDLE')

    def test_new_auto_task_waits_for_lead(self):
        p = team.create(self.root, 'AUTO-001', 'auto', 'full', True)
        self.assertEqual(team_runner.dispatch_once(self.root, dry_run=True), 'IDLE')
        state = team.read(p/'state.json')
        self.assertEqual(state['automation']['status'], 'WAITING_FOR_LEAD')

    def test_expected_role_is_selected_after_lead_advances(self):
        p = team.create(self.root, 'AUTO-002', 'auto', 'full', True)
        state = team.read(p/'state.json'); team.advance(self.root,p,state,'ANALYSIS'); team.atomic(p/'state.json',state)
        with patch.object(team_runner, 'command_for', return_value=(['fake'],'codex',None)):
            self.assertEqual(team_runner.dispatch_once(self.root, dry_run=True), 'DRY_RUN')
        self.assertEqual(team_runner.STAGE_ROLE['ANALYSIS'], 'analyst')

    def test_open_gate_stops_before_dispatch(self):
        p = team.create(self.root, 'AUTO-003', 'auto', 'risky', True)
        state = team.read(p/'state.json'); team.advance(self.root,p,state,'ANALYSIS')
        state['gates']=[{'id':'g1','kind':'HIGH_IMPACT','status':'AWAITING_USER'}]
        team.atomic(p/'state.json',state)
        self.assertEqual(team_runner.dispatch_once(self.root, dry_run=True), 'IDLE')
        self.assertEqual(team.read(p/'state.json')['automation']['status'], 'AWAITING_USER')

    def test_generated_commands_do_not_bypass_permissions(self):
        p = team.create(self.root, 'AUTO-004', 'auto', 'full', True)
        state = team.read(p/'state.json'); team.advance(self.root,p,state,'ANALYSIS')
        prompt = team_runner.workflow_prompt(self.root,'AUTO-004',state,'analyst','run-1')
        command, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'analyst',prompt,'run-1')
        rendered=' '.join(command)
        self.assertTrue(command[0].endswith('claude'))
        self.assertIn('sonnet', command)
        dev, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'developer',prompt,'run-1')
        self.assertNotIn('--model', dev)
        self.assertNotIn('dangerously-skip-permissions', rendered)
        self.assertNotIn('bypassPermissions', rendered)
        # A stale Codex session from before the engine switch is not resumed by Claude.
        state['sessions'] = {'analyst': {'provider': 'codex', 'session_id': 'old-codex-thread'}}
        command, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'analyst',prompt,'run-1')
        self.assertNotIn('old-codex-thread', command)
        self.assertNotIn('--resume', command)
        # Developer resumes only within the prompt-cache TTL; an older session starts fresh.
        fresh = team.now()
        state['sessions'] = {'developer': {'provider': 'claude', 'session_id': 'dev-1', 'updated': fresh}}
        command, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'developer',prompt,'run-1')
        self.assertIn('dev-1', command)
        state['sessions']['developer']['updated'] = '2020-01-01T00:00:00+00:00'
        command, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'developer',prompt,'run-1')
        self.assertNotIn('dev-1', command)
        self.assertNotIn('--resume', command)
        # Verifier stays on Codex: sandboxed, never bypassed.
        command, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'verifier',prompt,'run-1')
        rendered=' '.join(command)
        self.assertNotIn('dangerously-bypass', rendered)
        self.assertNotIn('-a never', rendered)
        self.assertIn('workspace-write', rendered)
        (self.root / '.ai/runner.json').write_text(json.dumps({'codex_args': ['-c', 'model_reasoning_effort="high"', '--disable', 'plugins']}))
        command, _, _ = team_runner.command_for(self.root,'AUTO-004',state,'verifier',prompt,'run-1')
        self.assertEqual(command[2:6], ['-c', 'model_reasoning_effort="high"', '--disable', 'plugins'])
        self.assertEqual(command[-1], prompt)

    def test_long_turn_warns_in_state_without_stopping_process(self):
        p = team.create(self.root, 'AUTO-005', 'long turn warning')

        class ImmediateTimer:
            # Fire deterministically so the unit test does not wait 25 minutes.
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.daemon = False

            def start(self):
                self.callback()

            def cancel(self):
                pass

        command = [sys.executable, '-c', 'print("{}")']
        with patch.object(team_runner.threading, 'Timer', ImmediateTimer):
            code, _, _ = team_runner.run_process(
                self.root, 'AUTO-005', 'developer', command, 'run-warning', 1500)
        self.assertEqual(code, 0)
        warning = team.read(p/'state.json')['automation']['warnings'][-1]
        self.assertEqual(warning['kind'], 'TURN_DURATION')
        self.assertEqual(warning['threshold_seconds'], 1500)
        self.assertEqual(warning['run_id'], 'run-warning')
        self.assertEqual(team_runner.DEFAULT_TURN_WARNING_MINUTES, 25.0)

    def test_silent_role_is_stopped_after_idle_timeout(self):
        p = team.create(self.root, 'AUTO-007', 'idle watchdog')
        # A role that never writes output must be stopped instead of hanging the runner.
        command = [sys.executable, '-c', 'import time; time.sleep(60)']
        with patch.object(team_runner, 'IDLE_CHECK_SECONDS', 0.05), \
             patch.object(team_runner, 'notify'):
            started = team_runner.time.monotonic()
            code, _, _ = team_runner.run_process(
                self.root, 'AUTO-007', 'verifier', command, 'run-idle', 1500, 0.3)
            elapsed = team_runner.time.monotonic() - started
        self.assertNotEqual(code, 0)
        self.assertLess(elapsed, 15)
        warning = team.read(p/'state.json')['automation']['warnings'][-1]
        self.assertEqual(warning['kind'], 'IDLE_TIMEOUT')
        self.assertEqual(warning['run_id'], 'run-idle')
        self.assertEqual(team_runner.DEFAULT_IDLE_TIMEOUT_MINUTES, 20.0)

    def test_chatty_role_is_not_stopped_by_idle_watchdog(self):
        team.create(self.root, 'AUTO-008', 'idle watchdog quiet')
        script = 'import time\nfor _ in range(8):\n    print("{}", flush=True); time.sleep(0.1)'
        with patch.object(team_runner, 'IDLE_CHECK_SECONDS', 0.05), \
             patch.object(team_runner, 'notify'):
            code, _, _ = team_runner.run_process(
                self.root, 'AUTO-008', 'developer', [sys.executable, '-c', script],
                'run-chatty', 1500, 0.5)
        self.assertEqual(code, 0)

    def test_running_role_sends_periodic_status(self):
        team.create(self.root, 'AUTO-009', 'status heartbeat')
        script = 'import time\nfor _ in range(6):\n    print("{}", flush=True); time.sleep(0.1)'
        with patch.object(team_runner, 'notify') as sent:
            team_runner.run_process(self.root, 'AUTO-009', 'developer',
                                    [sys.executable, '-c', script], 'run-status', 1500, 60, 0.15)
        self.assertGreaterEqual(sent.call_count, 2)
        self.assertIn('developer', sent.call_args[0][0])
        self.assertEqual(team_runner.DEFAULT_STATUS_MINUTES, 10.0)

    def test_dispatch_snapshots_markdown_and_restores_deleted_report(self):
        p = team.create(self.root, 'AUTO-006', 'snapshot restore', 'full', True)
        (p/'NOTES.md').write_text('non-canonical task note')
        original = (p/'ANALYSIS.md').read_text()
        state = team.read(p/'state.json')
        team.advance(self.root,p,state,'ANALYSIS'); team.atomic(p/'state.json',state)

        def delete_report_then_advance(root, ident, role, command, run_id,
                                       warning_seconds, idle_seconds, status_seconds):
            # Simulate a replace-style patch deleting the report before content is recreated.
            (p/'ANALYSIS.md').unlink()
            current = team.read(p/'state.json')
            team.advance(self.root,p,current,'DESIGN'); team.atomic(p/'state.json',current)
            log_path = root/'.ai'/'runner-logs'/ident/f'{role}-{run_id}.log'
            log_path.write_text('fixture')
            return 0, None, log_path

        with patch.object(team_runner, 'command_for', return_value=(['fake'],'codex',None)), \
             patch.object(team_runner, 'run_process', side_effect=delete_report_then_advance):
            self.assertEqual(team_runner.dispatch_once(self.root), 'ADVANCED')

        self.assertEqual((p/'ANALYSIS.md').read_text(), original)
        snapshots = list((self.root/'.ai/runner-logs/AUTO-006').glob('snapshot-*'))
        self.assertEqual(len(snapshots), 1)
        # The pre-turn snapshot covers every task Markdown, not just canonical reports.
        self.assertEqual((snapshots[0]/'NOTES.md').read_text(), 'non-canonical task note')


    def stage_task(self, ident, stage_role_stage, scope=None, profile='small'):
        p = team.create(self.root, ident, ident, profile, True)
        state = team.read(p/'state.json')
        while state['stage'] != stage_role_stage:
            path = state['stage_path']; team.advance(self.root, p, state, path[path.index(state['stage'])+1])
        team.atomic(p/'state.json', state)
        if scope:
            (p/'TASK.md').write_text('## Dosyalar\n' + ''.join(f'- {g}\n' for g in scope))
        return p

    def test_models_json_sets_role_models_and_is_optional(self):
        self.stage_task('MD-1', 'IMPLEMENTATION')
        state = team.read(team.task(self.root, 'MD-1')/'state.json')
        cmd = lambda role: team_runner.command_for(self.root, 'MD-1', state, role, 'p', 'r')[0]
        self.assertNotIn('--model', cmd('tester'))
        self.assertIn('sonnet', cmd('lead'))
        (self.root/'.ai/models.json').write_text(json.dumps(
            {'developer': 'opus', 'developer_small': 'sonnet', 'tester': 'haiku', 'lead': 'haiku'}))
        c = cmd('developer'); self.assertEqual(c[c.index('--model')+1], 'sonnet')  # small profile
        state['profile'] = 'full'
        c = cmd('developer'); self.assertEqual(c[c.index('--model')+1], 'opus')
        c = cmd('tester'); self.assertEqual(c[c.index('--model')+1], 'haiku')
        self.assertNotIn('--model', cmd('analyst'))  # unlisted role: CLI default

    def test_admission_rules(self):
        cfg = team_runner.runner_config(self.root)
        self.assertEqual(cfg['max_parallel'], 3)
        a = lambda running, cand, c=cfg: team_runner.admissible(running, cand, c)
        # disjoint scoped developers run together; unscoped developer is a global lock
        self.assertTrue(a([('A', 'developer', ['lib/a/**'])], ('B', 'developer', ['lib/b/**'])))
        self.assertFalse(a([('A', 'developer', ['lib/a/**'])], ('B', 'developer', ['lib/a/x/**'])))
        self.assertFalse(a([('A', 'developer', None)], ('B', 'developer', ['lib/b/**'])))
        self.assertFalse(a([('A', 'developer', None)], ('B', 'tester', ['lib/b/**'])))
        self.assertTrue(a([('A', 'developer', ['lib/a/**'])], ('B', 'tester', ['lib/b/**'])))
        # tester/verifier single; analyst runs beside an unscoped developer
        self.assertFalse(a([('A', 'tester', ['x/**'])], ('B', 'tester', ['y/**'])))
        self.assertFalse(a([('A', 'verifier', None)], ('B', 'verifier', None)))
        self.assertTrue(a([('A', 'developer', None)], ('B', 'analyst', None)))
        self.assertFalse(a([('A', 'analyst', None)], ('A', 'tester', None)))  # one role per task
        # max_parallel and project override
        full = [('A', 'analyst', None), ('B', 'designer', None), ('C', 'analyst', None)]
        self.assertFalse(a(full, ('D', 'designer', None)))
        (self.root/'.ai/runner.json').write_text(json.dumps({'max_parallel': 4}))
        self.assertTrue(a(full, ('D', 'designer', None), team_runner.runner_config(self.root)))

    def test_watch_runs_disjoint_tasks_concurrently(self):
        self.stage_task('PA-1', 'IMPLEMENTATION', ['lib/a/**'])
        self.stage_task('PA-2', 'IMPLEMENTATION', ['lib/b/**'])
        self.stage_task('PA-3', 'IMPLEMENTATION', ['lib/a/**'])
        started, release = [], team_runner.threading.Event()

        def fake(root, ident, state, role, dry_run, *timing):
            started.append(ident); release.wait(5); return 'ADVANCED'

        with patch.object(team_runner, 'run_task', side_effect=fake):
            running = team_runner.watch_loop(self.root, None, 0, (1, 1, 1), once_round=True)
            time_end = team_runner.time.time() + 3
            while len(started) < 2 and team_runner.time.time() < time_end:
                team_runner.time.sleep(0.02)
            release.set()
            for thread, _, _ in running.values(): thread.join(5)
        self.assertEqual(sorted(started), ['PA-1', 'PA-2'])  # PA-3 overlaps PA-1

    def test_limit_error_waits_then_resumes_instead_of_blocking(self):
        p = self.stage_task('LM-1', 'IMPLEMENTATION')
        log = self.root/'limit.log'
        log.write_text('{"type":"result"}\nYou\'ve hit your session limit · resets 9:10pm (Europe/Istanbul)\n')
        now = team_runner.datetime(2026, 10, 21, 17, 0, tzinfo=team_runner.timezone.utc)  # 20:00 Istanbul
        at = team_runner.limit_retry(log, now)
        self.assertEqual(at.astimezone(team_runner.timezone.utc).strftime('%H:%M'), '18:12')
        log.write_text('getaddrinfo ENOTFOUND api.anthropic.com')
        self.assertEqual(team_runner.limit_retry(log, now), now + team_runner.timedelta(minutes=2))  # network: fast retry
        log.write_text('{"type": "error", "message": "You’ve hit your usage limit. ... or try again at 9:31 PM."}')
        self.assertEqual(team_runner.limit_retry(log, now).astimezone().strftime('%H:%M'), '21:33')  # Codex wording
        log.write_text('Error: tests failed')
        self.assertIsNone(team_runner.limit_retry(log, now))

        def fake_run(root, ident, role, command, run_id, *rest):
            lp = root/'.ai'/'runner-logs'/ident/f'{role}-{run_id}.log'
            lp.write_text("You've hit your usage limit"); return 1, None, lp

        state = team.read(p/'state.json'); state['developer'] = {'session': 'dead', 'at': team.now()}
        team.atomic(p/'state.json', state)
        with patch.object(team_runner, 'command_for', return_value=(['fake'], 'codex', None)), \
             patch.object(team_runner, 'run_process', side_effect=fake_run), \
             patch.object(team_runner, 'notify'):
            self.assertEqual(team_runner.dispatch_once(self.root), 'WAITING_LIMIT')
            after = team.read(p/'state.json')
            self.assertEqual(after['automation']['status'], 'WAITING_LIMIT')
            self.assertIsNone(after['developer'])
            self.assertEqual(team_runner.dispatch_once(self.root), 'IDLE')  # still waiting
            after['automation']['retry_at'] = '2020-01-01T00:00:00+00:00'
            team.atomic(p/'state.json', after)
            self.assertEqual(team_runner.dispatch_once(self.root), 'WAITING_LIMIT')  # retried the same stage
        self.assertEqual(team.read(p/'state.json')['stage'], 'IMPLEMENTATION')

    def test_real_failure_still_blocks(self):
        p = self.stage_task('LM-2', 'IMPLEMENTATION')

        def fake_run(root, ident, role, command, run_id, *rest):
            lp = root/'.ai'/'runner-logs'/ident/f'{role}-{run_id}.log'
            lp.write_text('assertion failed'); return 1, None, lp

        with patch.object(team_runner, 'command_for', return_value=(['fake'], 'codex', None)), \
             patch.object(team_runner, 'run_process', side_effect=fake_run), \
             patch.object(team_runner, 'notify'):
            self.assertEqual(team_runner.dispatch_once(self.root), 'BLOCKED')
        self.assertEqual(team.read(p/'state.json')['automation']['status'], 'BLOCKED')

    def test_codex_verifier_gets_flutter_cache_writable_roots(self):
        fake = Path(self.tmp.name)/'sdk/bin'; (fake/'cache').mkdir(parents=True)
        (fake/'flutter').write_text('#!/bin/sh')
        with patch.object(team_runner.shutil, 'which', return_value=str(fake/'flutter')), \
             patch.object(team_runner.Path, 'home', return_value=Path(self.tmp.name)/'home'):
            roots = team_runner.codex_write_roots()
            self.assertEqual(roots, [str((fake/'cache').resolve())])
            (Path(self.tmp.name)/'home/.pub-cache').mkdir(parents=True)
            state = {'stage': 'VERIFY_CODEX', 'sessions': {}}
            with patch.object(team_runner, 'executable', return_value='codex'):
                cmd, _, _ = team_runner.command_for(self.root, 'X', state, 'verifier', 'p', 'r')
                self.assertEqual(cmd, ['codex', 'exec'] + cmd[2:])
        arg = next(c for c in cmd if c.startswith('sandbox_workspace_write.writable_roots='))
        self.assertIn(str((fake/'cache').resolve()), arg)
        self.assertIn('.pub-cache', arg)

    def test_install_agent_writes_only_service_file_and_prints_commands(self):
        home = Path(self.tmp.name)/'home'
        with patch.dict('os.environ', {'HOME': str(home), 'XDG_CONFIG_HOME': str(home/'.config')}), \
             patch.object(team_runner.subprocess, 'run') as ran:
            out = team_runner.install_agent(self.root, dry_run=True, platform='darwin')
            plist = home/'Library/LaunchAgents/com.ai-orchestra.runner.fixture.plist'
            self.assertIn('DRY-RUN', out); self.assertFalse(plist.exists())
            out = team_runner.install_agent(self.root, platform='darwin')
            data = team_runner.plistlib.loads(plist.read_bytes())
            self.assertTrue(data['RunAtLoad'] and data['KeepAlive'])
            self.assertEqual(data['StandardOutPath'], str(self.root/'.ai/runner-logs/_team_runner.log'))
            self.assertEqual(data['ProgramArguments'][-3:], ['--project', str(self.root), 'watch'])
            self.assertIn('PATH', data['EnvironmentVariables'])
            self.assertIn('launchctl bootstrap gui/$(id -u)', out)
            self.assertIn('bootout', team_runner.uninstall_agent(self.root, platform='darwin'))
            self.assertTrue(plist.exists())  # no --apply: kept
            team_runner.uninstall_agent(self.root, apply=True, platform='darwin')
            self.assertFalse(plist.exists())
            out = team_runner.install_agent(self.root, platform='linux')
            unit = home/'.config/systemd/user/ai-orchestra-runner-fixture.service'
            self.assertIn('Restart=always', unit.read_text()); self.assertIn('systemctl --user enable --now', out)
            ran.assert_not_called()  # never runs launchctl/systemctl itself

    def test_watch_waits_for_held_runner_lock_instead_of_exiting(self):
        import fcntl, io, contextlib
        held = (self.root/'.ai/.runner.lock').open('a'); fcntl.flock(held, fcntl.LOCK_EX)
        got, log = team_runner.threading.Event(), io.StringIO()
        with self.assertRaises(RuntimeError), team_runner.runner_lock(self.root):
            pass  # `once` still fails fast
        def watcher():
            with team_runner.runner_lock(self.root, wait=True):
                got.set()
        with patch.object(team_runner, 'LOCK_RETRY_SECONDS', 0.05), contextlib.redirect_stdout(log):
            th = team_runner.threading.Thread(target=watcher); th.start()
            team_runner.time.sleep(0.3); self.assertFalse(got.is_set())
            fcntl.flock(held, fcntl.LOCK_UN); held.close(); th.join(5)
        self.assertTrue(got.is_set())
        self.assertEqual(log.getvalue().count('Başka bir Team Runner çalışıyor'), 1)  # one line, not per retry



class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        iso = patch.dict('os.environ', {'XDG_CONFIG_HOME': self.tmp.name, 'CODEX_HOME': self.tmp.name, 'TEAM_NOW_OFFSET': '0'})
        iso.start(); self.addCleanup(iso.stop)
        import quota
        self.q = quota
        self.now = team.utcnow().timestamp()

    def log(self, *lines):
        f = Path(self.tmp.name) / 'role.log'
        f.write_text('\n'.join(l if isinstance(l, str) else json.dumps(l) for l in lines) + '\n')
        return f

    def test_interruption_reads_only_the_clis_own_errors(self):
        now = team_runner.datetime(2026, 10, 24, 12, 0, tzinfo=team_runner.timezone.utc)
        weekly = self.log({'type': 'result', 'is_error': True, 'api_error_status': 429,
                           'result': "You've hit your weekly limit · resets Oct 25 at 5pm (Europe/Istanbul)"})
        kind, at = team_runner.interruption(weekly, now)
        self.assertEqual((kind, at.astimezone(team_runner.timezone.utc).strftime('%m-%d %H:%M')), ('limit', '10-25 14:02'))
        tool_noise = self.log({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'server overloaded, ENOTFOUND'}]}},
                              {'type': 'result', 'is_error': False, 'result': 'BLOCKED: Docker yok'})
        self.assertEqual(team_runner.interruption(tool_noise, now), (None, None))  # a test's output is not our failure
        codex = self.log({'type': 'turn.failed', 'error': {'message': "You've hit your usage limit. Try again at 4:31 PM."}})
        self.assertEqual(team_runner.interruption(codex, now)[0], 'limit')
        later = team_runner.interruption(self.log({'type': 'turn.failed', 'error': {'message':
            "You’ve hit your usage limit. Visit https://x/settings/usage to purchase more credits or try again at Oct 30th, 2026 4:31 PM."}}), now)[1]
        self.assertEqual((later.astimezone().strftime('%Y-%m-%d'), later.minute), ('2026-10-30', 33))  # not "today 4:31"
        self.assertEqual(team_runner.interruption(self.log({'type': 'turn.failed', 'error': {'message':
            'Your workspace is out of credits. Try again later.'}}), now), ('limit', now + team_runner.LIMIT_FALLBACK + team_runner.timedelta(minutes=2)))
        exact = team_runner.interruption(self.log({'type': 'result', 'is_error': True, 'result': 'x'}), now, reset_epoch=now.timestamp() + 600)
        self.assertEqual(exact, ('limit', now + team_runner.timedelta(seconds=600 + 120)))
        net = self.log('API Error: getaddrinfo ENOTFOUND api.anthropic.com')
        self.assertEqual([team_runner.interruption(net, now, attempt=a)[1] - now for a in (1, 2, 9)],
                         [team_runner.timedelta(minutes=m) for m in (2, 5, 20)])

    def test_admission_counts_runs_in_flight_and_frees_at_the_reset(self):
        win = lambda u, reset: {'five_hour': (u, reset), 'seven_day': (0.3, self.now + 5 * 86400)}
        self.q.observe('claude', win(0.6, self.now + 3600))
        ok, _, _ = self.q.admit('claude', {'five_hour': 0.12, 'seven_day': 0.01})
        self.assertTrue(ok)
        self.q.reserve('r1', 'claude', {'five_hour': 0.3, 'seven_day': 0.01})
        ok, until, why = self.q.admit('claude', {'five_hour': 0.12, 'seven_day': 0.01})
        self.assertFalse(ok); self.assertEqual(until, int(self.now + 3600) + 60); self.assertIn('+%30 çalışan', why)
        self.q.observe('claude', win(0.8, self.now + 3600))  # r1 has used 0.2 of its 0.3 so far
        self.assertIn('+%10 çalışan', self.q.admit('claude', {'five_hour': 0.12, 'seven_day': 0.01})[2])
        self.q.release('r1')
        self.assertTrue(self.q.admit('claude', {'five_hour': 0.12, 'seven_day': 0.01})[0])
        self.q.observe('claude', win(0.9, self.now - 1), at=self.now + 1)  # window already reset: counts as empty
        self.assertTrue(self.q.admit('claude', {'five_hour': 0.5, 'seven_day': 0.01})[0])
        self.q.observe('codex', {'five_hour': (0.02, self.now + 900)})
        self.assertTrue(self.q.admit('codex', {'five_hour': 2.0})[0])  # an estimate above a whole window never deadlocks

    def test_claude_usage_command_refreshes_the_snapshot_without_a_model_call(self):
        report = {'type': 'assistant', 'usage_report': {'rate_limits': {'limits': [  # shape of a real /usage reply
            {'kind': 'session', 'group': 'session', 'percent': 61, 'resets_at': '2026-10-25T16:49:59.616086+00:00'},
            {'kind': 'weekly_all', 'group': 'weekly', 'percent': 100, 'resets_at': '2026-10-29T18:59:59+00:00'}]}}}
        fake = Path(self.tmp.name) / 'claude'
        fake.write_text(f"#!/bin/sh\necho 'Warning: no stdin'\necho '{json.dumps(report)}'\n"); fake.chmod(0o755)
        epoch = lambda iso: team_runner.datetime.fromisoformat(iso).timestamp()
        self.assertEqual(self.q.observe_claude(str(fake)), {'five_hour': (0.61, epoch('2026-10-25T16:49:59.616086+00:00')),
                                                            'seven_day': (1.0, epoch('2026-10-29T18:59:59+00:00'))})
        snap = self.q.load()['claude']
        self.assertEqual((snap['status'], snap['windows']['five_hour']['util']), ('rejected', 0.61))
        self.assertIsNone(self.q.observe_claude(str(Path(self.tmp.name) / 'missing')))

    def test_claude_and_codex_telemetry_map_to_the_same_windows(self):
        wins, status = self.q.claude_windows({'type': 'rate_limit_event', 'rate_limit_info': {
            'status': 'rejected', 'rateLimitType': 'seven_day', 'resetsAt': 99,
            'unifiedWindows': {'five_hour': {'utilization': 0.4, 'resetsAt': 50}, 'seven_day': {'utilization': 0.99, 'resetsAt': 99}}}})
        self.assertEqual((wins, status), ({'five_hour': (0.4, 50), 'seven_day': (1.0, 99)}, 'rejected'))
        self.assertEqual(self.q.codex_windows({'primary': {'used_percent': 36.0, 'window_minutes': 300, 'resets_at': 7},
                                               'secondary': {'used_percent': 15.0, 'window_minutes': 10080, 'resets_at': 8}}),
                         {'five_hour': (0.36, 7), 'seven_day': (0.15, 8)})

    def test_expected_cost_uses_measured_runs_after_five(self):
        root = Path(self.tmp.name)
        (root / '.ai').mkdir()
        self.assertEqual(self.q.expected(root, 'claude', 'developer', 'opus')['five_hour'], 0.12)
        for d in (0.05, 0.07, 0.06, 0.08, 0.06):
            team.metric(root, {'type': 'run', 'role': 'developer', 'model': 'claude-opus-5-5', 'quota': {'engine': 'claude', 'five_hour': d, 'seven_day': 0.01}})
        self.assertEqual(self.q.expected(root, 'claude', 'developer', 'opus')['five_hour'], 0.06)
        self.assertEqual(self.q.expected(root, 'claude', 'developer', 'sonnet')['five_hour'], 0.05)  # other model: default
        for d in (0.5, 0.4, 0.6, 0.5, 0.5):
            team.metric(root, {'type': 'run', 'role': 'verifier', 'model': 'gpt-5.6-sol', 'quota': {'engine': 'codex', 'five_hour': d}})
        self.assertEqual(self.q.expected(root, 'codex', 'verifier', 'gpt-5.6-sol')['five_hour'], 0.5)
        self.assertEqual(self.q.expected(root, 'codex', 'verifier', 'gpt-6.1-sol')['five_hour'], 0.25)

    def test_roles_and_services_bypass_terminal_shims(self):
        base = Path(self.tmp.name)
        shim, real = base / 'T/cmux-cli-shims/ABC', base / 'bin'
        for d in (shim, real):
            d.mkdir(parents=True); (d / 'codex').write_text('#!/bin/sh\n'); (d / 'codex').chmod(0o755)
        with patch.dict('os.environ', {'PATH': f'{shim}{team_runner.os.pathsep}{real}'}):
            self.assertEqual(team_runner.executable('codex'), str(real / 'codex'))
            self.assertEqual(team_runner.agent_env()['PATH'], str(real))


if __name__ == '__main__':
    unittest.main(verbosity=2)
