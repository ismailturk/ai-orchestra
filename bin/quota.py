"""Subscription quota governor shared by every runner on this machine.

Both engines report live window usage for free: Claude Code streams `rate_limit_event` (5 h and 7 d
windows) and Codex writes `rate_limits` (primary 5 h, secondary weekly) into its rollout files. The
runner records the latest snapshot here and, before starting a role, checks that the role's typical
cost still fits the window. A role that would die at the limit waits for the reset instead: a run cut
by a 429 loses its work (measured: 77 runs, ~$394 API-equivalent) and blocks the user's own Lead chat.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

import team

WINDOWS = ('five_hour', 'seven_day')
# Fallback cost per role run as a fraction of each window, measured on 1013 Claude runs and the Codex
# verifier rollouts of 2026-10 (median per role/model). Real metrics replace these after 5 runs.
DEFAULT_COST = {
    ('claude', 'developer', 'opus'): (0.12, 0.013), ('claude', 'developer', 'sonnet'): (0.05, 0.006),
    ('claude', 'tester', 'opus'): (0.12, 0.013), ('claude', 'tester', 'sonnet'): (0.04, 0.005),
    ('claude', 'analyst', ''): (0.065, 0.008), ('claude', 'designer', ''): (0.036, 0.004),
    ('claude', 'lead', ''): (0.02, 0.002), ('claude', 'verifier', ''): (0.04, 0.004),
    ('codex', 'verifier', ''): (0.25, 0.05),
}
# Headroom kept free per engine/window: the user's interactive Lead session shares the Claude window.
DEFAULT_RESERVE = {'claude': {'five_hour': 0.05, 'seven_day': 0.02}, 'codex': {'five_hour': 0.0, 'seven_day': 0.0}}
# Below this usage a role always starts: an estimate larger than a whole window must not deadlock.
ALWAYS_START = 0.05
SLACK_SECONDS = 60  # start this long after the reset, not on the second


def path():
    return team.config_dir() / 'quota.json'


@contextlib.contextmanager
def _locked():
    f = path()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f.with_suffix('.lock'), 'a') as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        yield


def load():
    try:
        data = team.read(path())
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _now():
    return team.utcnow().timestamp()


def _family(model):
    model = (model or '').lower()
    return next((f for f in ('opus', 'sonnet', 'haiku') if f in model), '')


def observe(engine, windows, status=None, at=None):
    """Record a usage snapshot {window: (utilization 0..1, resets_at epoch)}; an older one never wins."""
    windows = {w: v for w, v in (windows or {}).items() if w in WINDOWS and v and v[0] is not None}
    if not windows:
        return
    at = at or _now()
    with _locked():
        data = load()
        cur = data.get(engine) or {}
        if (cur.get('at') or 0) > at:
            return
        data[engine] = {'at': at, 'status': status or cur.get('status'),
                        'windows': {**(cur.get('windows') or {}),
                                    **{w: {'util': round(float(u), 4), 'resets_at': int(r or 0)} for w, (u, r) in windows.items()}}}
        team.atomic(path(), data)


def claude_windows(event):
    """{window: (util, resets_at)} and status from a Claude Code `rate_limit_event`, else (None, None)."""
    if not isinstance(event, dict) or event.get('type') != 'rate_limit_event':
        return None, None
    info = event.get('rate_limit_info') or {}
    wins = {w: ((v or {}).get('utilization'), (v or {}).get('resetsAt')) for w, v in (info.get('unifiedWindows') or {}).items()}
    if info.get('rateLimitType') in WINDOWS and info.get('utilization') is not None:
        wins[info['rateLimitType']] = (info['utilization'], info.get('resetsAt'))
    if info.get('status') == 'rejected' and info.get('rateLimitType') in WINDOWS:  # the cut window is full
        wins[info['rateLimitType']] = (max(1.0, wins.get(info['rateLimitType'], (0,))[0] or 0), info.get('resetsAt'))
    return wins, info.get('status')


def codex_windows(rate_limits):
    """Codex rollout `rate_limits` {primary, secondary} -> {window: (util, resets_at)}."""
    out = {}
    for key in ('primary', 'secondary'):
        v = (rate_limits or {}).get(key) or {}
        if v.get('used_percent') is None:
            continue
        window = 'seven_day' if (v.get('window_minutes') or 0) > 600 else 'five_hour'
        out[window] = (v['used_percent'] / 100, v.get('resets_at'))
    return out


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def _tail_rate_limits(f, first=False):
    """First or last `rate_limits` of a rollout file as (windows, epoch) or (None, None)."""
    try:
        with open(f, 'rb') as h:
            if not first:
                h.seek(0, os.SEEK_END); h.seek(max(0, h.tell() - 262_144))
            lines = h.read(262_144).decode(errors='replace').splitlines()
    except OSError:
        return None, None
    for line in (lines if first else reversed(lines)):
        if '"rate_limits"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        wins = codex_windows((e.get('payload') or {}).get('rate_limits'))
        if wins:
            ts = e.get('timestamp')
            try:
                at = datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() if ts else None
            except ValueError:
                at = None
            return wins, at
    return None, None


def codex_rollout(thread_id):
    """The rollout file of a Codex thread (its id ends the file name), newest days first."""
    if not thread_id:
        return None
    days = sorted((codex_home() / 'sessions').glob('*/*/*'), reverse=True)[:7]
    for d in days:
        hit = next(d.glob(f'rollout-*{thread_id}.jsonl'), None)
        if hit:
            return hit
    return None


def observe_codex():
    """Refresh the Codex snapshot from the newest rollout (any session of this account counts)."""
    days = sorted((codex_home() / 'sessions').glob('*/*/*'), reverse=True)[:2]
    files = sorted((f for d in days for f in d.glob('rollout-*.jsonl')), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:3]:
        wins, at = _tail_rate_limits(f)
        if wins:
            observe('codex', wins, 'rejected' if any(u >= 1 for u, _ in wins.values()) else 'allowed', at)
            return wins
    return None


def claude_usage_windows(report):
    """{window: (util, resets_at)} from the `usage_report` that Claude Code's /usage command streams."""
    out = {}
    for lim in ((report or {}).get('rate_limits') or {}).get('limits') or []:
        window = 'five_hour' if lim.get('group') == 'session' else 'seven_day' if lim.get('kind') == 'weekly_all' else None
        if window and lim.get('percent') is not None:
            reset = lim.get('resets_at')
            out[window] = (lim['percent'] / 100, datetime.fromisoformat(reset).timestamp() if reset else None)
    return out


def observe_claude(claude, timeout=60):
    """Refresh the Claude snapshot with Claude Code's own /usage command: no model call (measured: 0 tokens,
    $0, ~5 s). It also counts the user's interactive sessions, which role runs never see."""
    try:
        out = subprocess.run([claude, '-p', '/usage', '--output-format', 'stream-json', '--verbose',
                              '--no-session-persistence', '--strict-mcp-config'],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if '"usage_report"' in line:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                wins = claude_usage_windows(json.loads(line).get('usage_report'))
                if wins:
                    observe('claude', wins, 'rejected' if any(u >= 1 for u, _ in wins.values()) else 'allowed')
                    return wins
    return None


def current(engine, data=None):
    """{window: {'util', 'resets_at'}} with windows past their reset counted as empty."""
    snap = (data if data is not None else load()).get(engine) or {}
    now, out = _now(), {}
    for w, v in (snap.get('windows') or {}).items():
        reset = v.get('resets_at') or 0
        out[w] = {'util': 0.0 if reset and reset <= now else v.get('util') or 0.0, 'resets_at': reset}
    return out


def expected(root, engine, role, model=None):
    """Typical cost of one run of `role` as {window: fraction}: median of this machine's measured runs, else defaults."""
    fam = _family(model)
    seen = {w: [] for w in WINDOWS}
    for r in team.load_metrics(root):
        q = r.get('quota') if isinstance(r.get('quota'), dict) else None
        if not q or r.get('type') != 'run' or r.get('role') != role or q.get('engine') != engine:
            continue
        if engine == 'claude' and _family(r.get('model')) != fam:
            continue
        if engine == 'codex' and model and r.get('model') != model:  # a new verifier model starts from defaults
            continue
        for w in WINDOWS:
            d = q.get(w)
            if isinstance(d, (int, float)) and d >= 0:
                seen[w].append(d)
    base = DEFAULT_COST.get((engine, role, fam)) or DEFAULT_COST.get((engine, role, '')) or (0.05, 0.005)
    return {w: (statistics.median(seen[w][-20:]) if len(seen[w]) >= 5 else base[i]) for i, w in enumerate(WINDOWS)}


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _remaining(r, cur):
    """What an in-flight run is still expected to add (its use so far already shows in `cur`)."""
    out = {}
    for w in WINDOWS:
        c = cur.get(w) or {}
        base = r.get('base', {}).get(w) or {}
        spent = (c.get('util') or 0) - (base.get('util') or 0) if c.get('resets_at') == base.get('resets_at') else c.get('util') or 0
        out[w] = max(0.0, (r.get('expect') or {}).get(w, 0) - max(0.0, spent))
    return out


def admit(engine, expect, reserve=None, data=None):
    """(True, None, '') when `expect` fits every window next to the runs already in flight, else
    (False, epoch to retry at, reason). Unknown usage admits: telemetry arrives with the first run."""
    data = data if data is not None else load()
    cur = current(engine, data)
    reserve = {**DEFAULT_RESERVE.get(engine, {}), **(reserve or {})}
    snap = data.get(engine) or {}
    flying = [_remaining(r, cur) for r in (data.get('inflight') or {}).values()
              if r.get('engine') == engine and _alive(r.get('pid'))]
    for w in WINDOWS:
        c = cur.get(w)
        if not c:
            continue
        used = c['util'] + sum(f[w] for f in flying)
        rejected = snap.get('status') == 'rejected' and c['util'] >= 1
        if rejected or (c['util'] > ALWAYS_START and used + expect.get(w, 0) > 1 - reserve.get(w, 0)):
            label = '5 saat' if w == 'five_hour' else 'haftalık'
            why = (f"{engine} {label} penceresi %{c['util'] * 100:.0f}" + (f" (+%{(used - c['util']) * 100:.0f} çalışan)" if used > c['util'] else '')
                   + f"; bu rol ~%{expect.get(w, 0) * 100:.0f} ister")
            return False, (c['resets_at'] or _now() + 1200) + SLACK_SECONDS, why
    return True, None, ''


def reserve(run_id, engine, expect, pid=None):
    with _locked():
        data = load()
        cur = current(engine, data)
        live = {k: v for k, v in (data.get('inflight') or {}).items() if _alive(v.get('pid'))}
        live[run_id] = {'engine': engine, 'expect': expect, 'pid': pid or os.getpid(), 'at': _now(),
                        'base': {w: dict(cur[w]) for w in cur}}
        data['inflight'] = live
        team.atomic(path(), data)


def release(run_id):
    with _locked():
        data = load()
        if run_id in (data.get('inflight') or {}):
            del data['inflight'][run_id]
            team.atomic(path(), data)


def window_delta(first, last):
    """Per-window use between two snapshots of one run; a window that reset in between is skipped."""
    out = {}
    for w in WINDOWS:
        a, b = (first or {}).get(w), (last or {}).get(w)
        if a and b and a[1] == b[1] and b[0] is not None and a[0] is not None:
            out[w] = round(max(0.0, b[0] - a[0]), 4)
    return out


def summary(engine, data=None):
    """One-line usage text for status displays, e.g. 'Claude 5s %62 ↻18:40 · 7g %31'."""
    cur = current(engine, data)
    if not cur:
        return ''
    parts = []
    for w, label in (('five_hour', '5s'), ('seven_day', '7g')):
        c = cur.get(w)
        if c:
            reset = datetime.fromtimestamp(c['resets_at']).strftime('%H:%M' if w == 'five_hour' else '%a %H:%M') if c['resets_at'] and c['util'] else '-'
            parts.append(f"{label} %{c['util'] * 100:.0f} ↻{reset}")
    return f"{'Claude' if engine == 'claude' else 'Codex'} " + ' · '.join(parts)
