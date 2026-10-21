"""End-to-end runner scenarios with fake `claude`/`codex` executables; no network, no real agents."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import team
import team_runner

# One script serves both engines (argv[0] tells which). Per task/role, FAKE_PLAN_DIR/<ID>.<role>
# holds whitespace-separated modes consumed one per attempt: ok | fail | limit | enotfound | crash.
FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
engine = Path(sys.argv[0]).name
root = Path.cwd(); ident = os.environ['AI_TEAM_TASK']; role = os.environ['AI_TEAM_ROLE']; run = os.environ['AI_TEAM_RUN_ID']
plans = Path(os.environ['FAKE_PLAN_DIR']); plan = plans / f'{ident}.{role}'
mode = 'ok'
if plan.is_file():
    modes = plan.read_text().split()
    if modes:
        mode = modes[0]; plan.write_text(' '.join(modes[1:]))
if mode == 'crash':
    sys.exit(137)  # killed: no output at all
out = lambda ev: print(json.dumps(ev), flush=True)
if engine == 'codex':
    print('Reading additional input from stdin...', flush=True)
    out({'type': 'thread.started', 'thread_id': 'thr-' + run}); out({'type': 'turn.started'})
else:
    out({'type': 'system', 'subtype': 'init', 'session_id': run, 'model': 'claude-fake'})
if mode == 'limit':
    out({'type': 'result', 'subtype': 'success', 'is_error': True,
         'result': "You've hit your session limit · resets 9:10pm"}); sys.exit(1)
if mode == 'enotfound':
    print('API Error: getaddrinfo ENOTFOUND api.anthropic.com', flush=True); sys.exit(1)

def team(*args):
    r = subprocess.run([sys.executable, os.environ['FAKE_TEAM'], '--project', str(root), *args],
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stderr, flush=True); sys.exit(r.returncode)

task = root / 'tasks' / ident
state = json.loads((task / 'state.json').read_text())
path = state['stage_path']; nxt = path[path.index(state['stage']) + 1]
report = lambda status: f'STATUS: {status}\nKomut: fake test\nOrtam: e2e\nSonuc: {status}\n' + 'ayrinti satiri\n' * 12
if role == 'developer':
    team('claim', ident, '--session', run)
    files = (plans / f'{ident}.files').read_text().split() if (plans / f'{ident}.files').is_file() else [f'src/{ident}.txt']
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True); (root / rel).write_text(f'{ident} {run}\n')
    (task / 'IMPLEMENTATION.md').write_text('STATUS: DONE\n' + ' '.join(files))
    team('release', ident, '--session', run); team('advance', ident, nxt)
elif role == 'tester' and mode == 'fail':
    (task / 'TEST_CLAUDE.md').write_text(report('FAIL'))
    team('evidence', ident, '--role', 'tester', '--result', 'FAIL', '--session', run, '--note', 'fake fail')
    team('rework', ident, '--reason', 'fake test failure')
elif role == 'tester':
    (task / 'TEST_CLAUDE.md').write_text(report('PASS'))
    team('evidence', ident, '--role', 'tester', '--result', 'PASS', '--session', run, '--note', 'fake pass')
    team('advance', ident, nxt)
elif role == 'verifier':
    (task / 'VERIFY_CODEX.md').write_text(report('PASS') + '## Bağımsız kontroller\n- fake kontrol\n')
    (task / 'FINAL_REPORT.md').write_text(report('PASS'))
    team('evidence', ident, '--role', 'verifier', '--result', 'PASS', '--session', run, '--note', 'fake verify')
    team('advance', ident, nxt)
elif role == 'lead':
    with (root / '.ai/CURRENT_STATE.md').open('a') as f: f.write(f'\n{ident} kapandi')
    team('knowledge', ident, '--summary', 'fake knowledge'); team('advance', ident, nxt)
if engine == 'codex':
    out({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'bitti'}})
    out({'type': 'turn.completed', 'usage': {'input_tokens': 500, 'cached_input_tokens': 400,
                                             'output_tokens': 30, 'reasoning_output_tokens': 5}})
else:
    out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'bitti'}]}})
    out({'type': 'result', 'subtype': 'success', 'num_turns': 3, 'total_cost_usd': 0.01,
         'usage': {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 300,
                   'cache_creation_input_tokens': 40}, 'modelUsage': {'claude-fake': {}}})
'''

TIMING = (1500, 600, 600)


class E2E(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / 'proj'; self.root.mkdir()
        team.init_project(self.root)
        (self.root / '.ai/project.json').write_text(json.dumps({'id': 'e2e', 'source_roots': [str(self.root)]}))
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        fakebin = base / 'fakebin'; fakebin.mkdir()
        for name in ('claude', 'codex'):
            (fakebin / name).write_text(FAKE); (fakebin / name).chmod(0o755)
        self.plans = base / 'plans'; self.plans.mkdir()
        env = {'PATH': f'{fakebin}{os.pathsep}{os.environ["PATH"]}', 'FAKE_TEAM': str(BIN / 'team.py'),
               'FAKE_PLAN_DIR': str(self.plans), 'TEAM_NOW_OFFSET': '0'}
        p = patch.dict(os.environ, env); p.start(); self.addCleanup(p.stop)
        for k in ('TEAM_VERIFIER_ENGINE', 'CMUX_WORKSPACE_ID', 'CMUX_SURFACE_ID', 'AI_TEAM_RUN_ID'):
            os.environ.pop(k, None)
        p = patch.object(team_runner, 'CLAUDE_ROLES', {'lead', 'analyst', 'designer', 'developer', 'tester'})
        p.start(); self.addCleanup(p.stop)
        self.log = io.StringIO()  # runner log (stamp output from all threads)
        out = contextlib.redirect_stdout(self.log); out.__enter__(); self.addCleanup(out.__exit__, None, None, None)

    def task(self, ident, profile='small', scope=None, plan=None, files=None):
        p = team.create(self.root, ident, 'Title ' + ident, profile, True, scope)
        with team.locked(self.root):  # Lead's first step: NEW -> first work stage
            s = team.read(p / 'state.json'); team.advance(self.root, p, s, s['stage_path'][1]); team.atomic(p / 'state.json', s)
        for role, modes in (plan or {}).items():
            (self.plans / f'{ident}.{role}').write_text(modes)
        if files:
            (self.plans / f'{ident}.files').write_text(' '.join(files))
        return p

    def round(self):
        return team_runner.watch_loop(self.root, None, 0, TIMING, once_round=True)

    def drive(self, rounds=40):
        for _ in range(rounds):
            running = self.round()
            if not running:
                return
            for thread, _, _ in running.values():
                thread.join(30)

    def state(self, ident):
        return team.read(self.root / 'tasks' / ident / 'state.json')

    def metrics(self):
        f = self.root / '.ai/metrics.jsonl'
        return [json.loads(l) for l in f.read_text().splitlines()] if f.is_file() else []

    def test_a_small_task_runs_to_done_by_itself(self):
        self.task('APP-001')
        self.drive()
        s = self.state('APP-001')
        self.assertEqual((s['stage'], s['automation']['status']), ('DONE', 'COMPLETED'), self.log.getvalue())
        self.assertEqual(s['changed_files'], ['src/APP-001.txt'])
        self.assertEqual(s['independence'], 'independent')
        self.assertEqual(s['codex_verify']['engine_id'], 'codex'); self.assertEqual(s['claude_test']['engine_id'], 'claude')
        self.assertIn('PASS/PASS (bağımsız)', self.log.getvalue())

    def test_b_fix_done_changelog_and_risk_escalation(self):
        cfg = team.read(self.root / '.ai/runner.json'); cfg['risk_paths'] = ['core/**']
        team.atomic(self.root / '.ai/runner.json', cfg)
        self.task('APP-002', 'fix')
        self.task('APP-003', 'fix', files=['core/db.txt'])
        self.drive()
        a, b = self.state('APP-002'), self.state('APP-003')
        self.assertEqual((a['stage'], a['profile'], a['independence']), ('DONE', 'fix', 'single-model'))
        self.assertIsNone(a['codex_verify'])
        log = (self.root / '.ai/CHANGELOG.md').read_text()
        self.assertIn('APP-002 — Title APP-002 — PASS', log); self.assertNotIn('APP-003', log)
        self.assertEqual((b['stage'], b['profile']), ('DONE', 'small'))
        self.assertEqual(b['escalated']['files'], ['core/db.txt'])
        self.assertEqual(b['codex_verify']['result'], 'PASS')
        self.assertIn('YÜKSELTİLDİ', self.log.getvalue())

    def wait_then_resume(self, ident, mode, offset):
        self.task(ident, plan={'developer': mode})
        self.drive()
        s = self.state(ident)
        self.assertEqual(s['automation']['status'], 'WAITING_LIMIT', self.log.getvalue())
        self.assertGreater(team_runner.datetime.fromisoformat(s['automation']['retry_at']), team.utcnow())
        self.assertEqual(s['stage'], 'IMPLEMENTATION')
        os.environ['TEAM_NOW_OFFSET'] = str(offset)  # time passes without waiting
        self.drive()
        self.assertEqual(self.state(ident)['stage'], 'DONE', self.log.getvalue())

    def test_c_session_limit_waits_until_reset_then_finishes(self):
        self.wait_then_resume('APP-004', 'limit', 2 * 86400)

    def test_d_network_error_waits_then_finishes(self):
        self.wait_then_resume('APP-005', 'enotfound', 3600)

    def test_e_tester_fail_reworks_then_blocks_after_three_rounds(self):
        self.task('APP-006', plan={'tester': 'fail fail fail fail fail'})
        self.drive()
        s = self.state('APP-006')
        self.assertEqual(s['automation']['status'], 'BLOCKED')
        self.assertEqual(s['rework_count'], 4)
        self.assertIn('3 düzeltme turu aşıldı', s['automation']['message'])

    def test_f_crashed_role_blocks_only_its_task(self):
        self.task('APP-007', plan={'developer': 'crash'})
        self.task('APP-008')
        self.drive()
        s = self.state('APP-007')
        self.assertEqual(s['automation']['status'], 'BLOCKED'); self.assertIn('137', s['automation']['message'])
        self.assertEqual(self.state('APP-008')['stage'], 'DONE')

    def test_g_disjoint_scoped_developers_start_together(self):
        self.task('APP-009', scope=['lib/a/**'], files=['lib/a/x.txt'])
        self.task('APP-010', scope=['lib/b/**'], files=['lib/b/x.txt'])
        self.task('APP-011', scope=['lib/a/**'], files=['lib/a/y.txt'])
        first = self.round()
        self.assertEqual(sorted(first), ['APP-009', 'APP-010'])  # APP-011 overlaps APP-009
        for thread, _, _ in first.values():
            thread.join(30)
        self.drive()
        for ident in ('APP-009', 'APP-010', 'APP-011'):
            self.assertEqual(self.state(ident)['stage'], 'DONE', ident)
        self.assertEqual(self.state('APP-011')['changed_files'], ['lib/a/y.txt'])

    def test_h_unreadable_state_is_skipped_not_fatal(self):
        bad = self.task('APP-012')
        self.task('APP-013')
        (bad / 'state.json').chmod(0)
        try:
            self.drive(5)  # must not raise
            self.assertIn('APP-012: ATLANDI', self.log.getvalue())
            # Fail-closed: the other Developer waits for the unknown lock instead of failing its claim.
            self.assertNotEqual(self.state('APP-013')['automation']['status'], 'BLOCKED')
        finally:
            (bad / 'state.json').chmod(0o644)
        with patch.object(team_runner, 'triage', side_effect=OSError('disk')):
            self.assertEqual(self.round(), {})  # per-task error: skipped, loop alive
        self.drive()
        for ident in ('APP-012', 'APP-013'):
            self.assertEqual(self.state(ident)['stage'], 'DONE', ident)


if __name__ == '__main__':
    unittest.main(verbosity=2)
