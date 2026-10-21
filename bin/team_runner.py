#!/usr/bin/env python3
"""Visible, fail-closed role orchestrator for onboarded AI-Team projects."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import plistlib
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import team


STAGE_ROLE = {
    'ANALYSIS': 'analyst',
    'DESIGN': 'designer',
    'IMPLEMENTATION': 'developer',
    'TEST_CLAUDE': 'tester',
    'VERIFY_CODEX': 'verifier',
    'KNOWLEDGE_UPDATE': 'lead',
}
# Analyst/Designer run on Claude; the verifier stays on Codex for model independence.
CLAUDE_ROLES = {'lead', 'analyst', 'designer', 'developer', 'tester'}
# TEAM_VERIFIER_ENGINE=claude (e.g. when Codex is unavailable) runs the
# verifier as a fresh, independent Claude session instead (same VERIFY_CODEX stage/docs).
if os.environ.get('TEAM_VERIFIER_ENGINE') == 'claude':
    CLAUDE_ROLES = CLAUDE_ROLES | {'verifier'}
CMUX = '/Applications/cmux.app/Contents/Resources/bin/cmux'
# Warn before typical session limits; the role keeps running so Lead can arrange a handoff.
DEFAULT_TURN_WARNING_MINUTES = 25.0
# A role that writes no output for this long is treated as hung: it is stopped and the task
# goes BLOCKED so Lead can resume it.
DEFAULT_IDLE_TIMEOUT_MINUTES = 20.0
IDLE_CHECK_SECONDS = 15.0
# While a role runs, the user gets a cmux status notification this often.
DEFAULT_STATUS_MINUTES = 10.0


_last_line = ['']
_warned = {}


def stamp(message):
    _last_line[0] = message
    print(time.strftime('[%H:%M:%S]'), message, flush=True)


def warn_once(key, message):
    """Log a recurring per-task problem once instead of on every poll."""
    if _warned.get(key) != message:
        _warned[key] = message
        stamp(message)


def render_event(role, event):
    """Rol olayini panele insan okunur bicimde basar.

    Log dosyasi ham JSON kalir; bu yalnizca canli izleme icindir. Sessiz bir
    panel rolun takilip takilmadigini gostermiyordu.
    """
    kind = event.get('type')
    item = event.get('item') or {}
    itype = item.get('type')
    if kind == 'thread.started':
        stamp(f'{role} · oturum {event.get("thread_id", "?")}')
    elif kind == 'item.started' and itype == 'command_execution':
        lines = (item.get('command') or '').strip().splitlines()
        if lines:
            text = lines[0].strip()
            if len(lines) > 1:
                text += f' … (+{len(lines) - 1} satır)'
            stamp(f'{role} $ {text[:300]}')
    elif kind == 'item.completed' and itype == 'command_execution':
        code = item.get('exit_code')
        if code not in (None, 0):
            stamp(f'{role} ! çıkış kodu {code}')
    elif kind == 'item.completed' and itype == 'agent_message':
        for line in (item.get('text') or '').strip().splitlines():
            if line.strip():
                stamp(f'{role} › {line.strip()[:300]}')
    elif kind == 'item.completed' and itype in {'file_change', 'patch_apply'}:
        detail = item.get('status') or item.get('path') or ''
        stamp(f'{role} · dosya değişikliği {detail}'.rstrip())
    else:
        render_claude_event(role, event)


def render_claude_event(role, event):
    """Claude Code --output-format stream-json olaylari.

    Codex ve Claude ayri olay bicimleri yayinlar; ikisi de ayni panele akar.
    """
    kind = event.get('type')
    if kind == 'system' and event.get('subtype') == 'init':
        stamp(f'{role} · oturum {event.get("session_id", "?")}')
    elif kind == 'assistant':
        for block in (event.get('message') or {}).get('content') or []:
            btype = block.get('type')
            if btype == 'text':
                for line in (block.get('text') or '').strip().splitlines():
                    if line.strip():
                        stamp(f'{role} › {line.strip()[:300]}')
            elif btype == 'tool_use':
                stamp(f'{role} $ {tool_summary(block)[:300]}')
    elif kind == 'user':
        for block in (event.get('message') or {}).get('content') or []:
            if block.get('type') == 'tool_result' and block.get('is_error'):
                text = str(block.get('content') or '').strip().replace('\n', ' ')
                stamp(f'{role} ! {text[:300]}')
    elif kind == 'result':
        subtype = event.get('subtype', '?')
        turns = event.get('num_turns')
        detail = f' · {turns} tur' if turns else ''
        stamp(f'{role} · sonuç: {subtype}{detail}')


def tool_summary(block):
    """Bir tool_use blogunu tek satirda ozetler."""
    name = block.get('name') or '?'
    data = block.get('input') or {}
    for key in ('command', 'file_path', 'pattern', 'path', 'url', 'prompt'):
        value = data.get(key)
        if value:
            text = str(value).strip().splitlines()
            head = text[0] if text else ''
            if len(text) > 1:
                head += f' … (+{len(text) - 1} satır)'
            return f'{name}: {head}'
    return name


def load_state(root, ident):
    p = team.task(root, ident)
    return p, team.read(p / 'state.json')


def update_runner_state(root, ident, **values):
    with team.locked(root):
        p, state = load_state(root, ident)
        auto = state.setdefault('automation', {})
        auto.update(values, updated=team.now())
        state['updated'] = team.now()
        state['revision'] = state.get('revision', 0) + 1
        team.atomic(p / 'state.json', state)
        return state


def save_session(root, ident, role, provider, session_id, run_id):
    with team.locked(root):
        p, state = load_state(root, ident)
        sessions = state.setdefault('sessions', {})
        sessions[role] = {
            'provider': provider,
            'session_id': session_id,
            'run_id': run_id,
            'updated': team.now(),
        }
        state['updated'] = team.now()
        state['revision'] = state.get('revision', 0) + 1
        team.atomic(p / 'state.json', state)


def record_idle_timeout(root, ident, role, run_id, idle_seconds):
    """Record why the watchdog stopped a role; the exit handler then marks the task BLOCKED."""
    with team.locked(root):
        p, state = load_state(root, ident)
        auto = state.setdefault('automation', {})
        auto.setdefault('warnings', []).append({
            'kind': 'IDLE_TIMEOUT',
            'role': role,
            'run_id': run_id,
            'threshold_seconds': idle_seconds,
            'created': team.now(),
        })
        auto.update(message=f'{role} {idle_seconds / 60:g} dk çıktı üretmedi; runner durdurdu',
                    updated=team.now())
        state['updated'] = team.now()
        state['revision'] = state.get('revision', 0) + 1
        team.atomic(p / 'state.json', state)


def record_turn_warning(root, ident, role, run_id, warning_seconds):
    """Append a durable warning without changing the running role or stage."""
    with team.locked(root):
        p, state = load_state(root, ident)
        auto = state.setdefault('automation', {})
        note = {
            'kind': 'TURN_DURATION',
            'role': role,
            'run_id': run_id,
            'threshold_seconds': warning_seconds,
            'created': team.now(),
        }
        # Keep prior warnings as audit notes; one timer writes at most once per role turn.
        auto.setdefault('warnings', []).append(note)
        auto.update(message=f'{role} turu süre eşiğini aştı; Lead devir hazırlamalı',
                    updated=team.now())
        state['updated'] = team.now()
        state['revision'] = state.get('revision', 0) + 1
        team.atomic(p / 'state.json', state)
        return note


def snapshot_task_markdown(root, ident, run_id):
    """Copy task Markdown before a role can replace a canonical report."""
    task_dir = team.task(root, ident)
    snapshot_dir = root/'.ai'/'runner-logs'/ident/f'snapshot-{team.slug(run_id)}'
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    # All task Markdown is retained; restoration remains limited to canonical reports.
    for source in sorted(task_dir.glob('*.md')):
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, snapshot_dir/source.name)
    return snapshot_dir


def restore_missing_canonical_reports(root, ident, snapshot_dir):
    """Restore only canonical reports that existed before the role turn."""
    task_dir = team.task(root, ident)
    restored = []
    for doc in team.DOCS:
        name = doc + '.md'
        source = snapshot_dir/name
        target = task_dir/name
        if source.is_file() and not target.exists() and not target.is_symlink():
            shutil.copy2(source, target)
            restored.append(name)
    return restored


def record_changes(root, ident, before):
    """Add the files this Developer run touched to the task's changed_files."""
    after = team.git_changes(root)
    with team.locked(root):
        p, state = load_state(root, ident)
        team.add_changed(p, state, before, after)
        team.atomic(p / 'state.json', state)


def notify(title, body):
    if not os.environ.get('CMUX_WORKSPACE_ID') or not Path(CMUX).is_file():
        return
    subprocess.run([CMUX, 'notify', '--title', title, '--body', body],
                   text=True, capture_output=True)


def workflow_prompt(root, ident, state, role, run_id):
    path = state.get('stage_path') or team.WORKFLOWS[state.get('profile', 'full')]
    stage = state['stage']
    next_stage = path[path.index(stage) + 1] if path.index(stage) + 1 < len(path) else None
    tool = Path(team.__file__).resolve()
    base = ['python3', str(tool), '--project', str(root)]

    def command(*parts):
        import shlex
        return shlex.join([*base, *parts])

    common = f"""
Bu çalışma Team Runner tarafından otomatik başlatıldı.
Görev: {ident}. Aktif aşama: {stage}. Çalışma kimliği: {run_id}.
Yalnız bu aşamayı tamamla. Human gate gerektiren hiçbir işlemi çalıştırma; gate kaydı oluşturup dur.
Başarı komutlarını ancak ilgili belge eksiksiz yazıldıktan ve doğrulandıktan sonra çalıştır.
Komut başarısız olursa aşamayı elle atlama. Kullanıcıya soru sorma; engeli Lead mesajına ve rapora yazıp çık.
""".strip()

    if role == 'analyst':
        protocol = f"""
ANALYSIS.md dosyasını kaynak yolları, kapsam, risk ve doğrulanmış bulgularla doldur; üretim koduna dokunma.
Lead ve Designer için mesaj bırak. Tamamlanınca yalnız şu komutla ilerle:
{command('advance', ident, next_stage)}
"""
    elif role == 'designer':
        protocol = f"""
DESIGN.md dosyasını akış, durumlar, erişilebilirlik ve kabul kriterleriyle doldur; üretim koduna dokunma.
Lead ve Developer için mesaj bırak. Tamamlanınca yalnız şu komutla ilerle:
{command('advance', ident, next_stage)}
"""
    elif role == 'developer':
        protocol = f"""
Kaynak düzenlemeden önce kilit al:
{command('claim', ident, '--session', run_id)}
Yalnız atanmış üretim kodunu değiştir ve IMPLEMENTATION.md dosyasını doldur. DB/backend sınırlarını ve human gate kurallarını koru.
Bitince kilidi bırak, Tester mesajını yaz ve aşamayı ilerlet:
{command('release', ident, '--session', run_id)}
{command('advance', ident, next_stage)}
Hata halinde de kilidi bırak; tamamlanmamış işi ilerletme.
"""
    elif role == 'tester':
        protocol = f"""
Güvenli sırayla test et; üretim kodunu düzeltme. TEST_CLAUDE.md içindeki STATUS, komut, ortam, sonuç ve sınırlamaları gerçek sonuçlarla yaz.
PASS ise sırasıyla kanıtı kaydet ve ilerle:
{command('evidence', ident, '--role', 'tester', '--result', 'PASS', '--session', run_id, '--note', 'Team Runner Claude test run; ayrintilar TEST_CLAUDE.md')}
{command('advance', ident, next_stage)}
FAIL ise FAIL kanıtı kaydet, Developer/Lead mesajı bırak ve rework komutuyla IMPLEMENTATION'a dön. BLOCKED ise BLOCKED kanıtı kaydet ve aşamayı değiştirme.
"""
    elif role == 'verifier':
        protocol = f"""
Tester raporunu kopyalama. Aynı kaynak sürümünde kritik testleri bağımsız yeniden çalıştır; negatif ve kenar durumlarını incele.
Kabul kriterlerinden en az bir kontrolü/testi KENDİN türet ve çalıştır (Tester'ın komutunun tekrarı sayılmaz);
VERIFY_CODEX.md içinde "## Bağımsız kontroller" başlığı altında komut ve sonucuyla listele.
VERIFY_CODEX.md ve taslak FINAL_REPORT.md dosyalarını doldur; üretim kodunu düzeltme.
PASS ise sırasıyla kanıtı kaydet ve ilerle:
{command('evidence', ident, '--role', 'verifier', '--result', 'PASS', '--session', run_id, '--note', ('Team Runner independent ' + ('Claude' if 'verifier' in CLAUDE_ROLES else 'Codex') + ' verification; ayrintilar VERIFY_CODEX.md'))}
{command('advance', ident, next_stage)}
FAIL ise FAIL kanıtı kaydet, Developer/Lead mesajı bırak ve rework komutuyla IMPLEMENTATION'a dön. BLOCKED ise aşamayı değiştirme.
"""
    elif role == 'lead':
        protocol = f"""
Bu aşama yalnız kapanış ve kalıcı bilgi güncellemesidir. Üretim kodu yazma.
.ai/CURRENT_STATE.md dosyasını EKLEME YAPMADAN kısa güncel özet olarak yeniden yaz (en fazla ~2 KB; eski ayrıntı .ai/archive/ altına, yalnız gerekiyorsa). .ai/KNOWN_ISSUES.md'de yalnız AÇIK kayıtlar kalsın; kapananı .ai/archive/KNOWN_ISSUES_*.md dosyasına taşı. Gerçekten değişen diğer knowledge belgelerini güncelle; FINAL_REPORT.md dosyasını iki bağımsız test, riskler ve sınırlamalarla tamamla.
Sonra sırasıyla:
{command('knowledge', ident, '--summary', f'{ident} tamamlandi; ayrintilar FINAL_REPORT.md ve CURRENT_STATE.md')}
{command('advance', ident, next_stage)}
Gate veya eksik kanıt varsa kapatma; Lead mesajı bırakıp dur.
"""
    else:
        raise ValueError('Unsupported runner role: ' + role)
    return team.role_prompt(root, role, ident) + '\n\n' + common + '\n' + protocol.strip()


def executable(name):
    found = shutil.which(name)
    if found:
        return found
    if name == 'codex':
        fallback = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
        if fallback.is_file():
            return str(fallback)
    raise ValueError(name + ' executable not found')


RESUME_MAX_AGE = timedelta(minutes=55)


def session_stale(updated):
    try:
        last = datetime.fromisoformat(updated)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - last > RESUME_MAX_AGE


def role_model(root, state, role):
    f = root / '.ai' / 'models.json'
    if not f.is_file():
        return 'sonnet' if role in {'analyst', 'designer', 'lead'} else None
    models = team.read(f)
    if role == 'developer' and state.get('profile') in {'tweak', 'small', 'fix'} and models.get('developer_small'):
        return models['developer_small']
    return models.get(role)


def codex_write_roots():
    """Dirs the Codex sandbox must write for Flutter/Dart tooling (only those that exist)."""
    roots = []
    flutter = shutil.which('flutter')
    if flutter:
        roots.append(Path(flutter).resolve().parent / 'cache')
    home = Path.home()
    roots += [home / '.pub-cache', home / '.dart-tool', home / '.flutter']
    return [str(r) for r in roots if r.is_dir()]


def command_for(root, ident, state, role, prompt, run_id):
    session = state.get('sessions', {}).get(role, {})
    previous = session.get('session_id')
    # A role that switched engine must not resume the other engine's session.
    if session.get('provider') not in (None, 'claude' if role in CLAUDE_ROLES else 'codex'):
        previous = None
    # Token saving: past the 1 h prompt-cache TTL a resume re-writes the
    # whole transcript to cache ($3-5 per short rework); a fresh session reading the task docs is cheaper.
    if previous and session_stale(session.get('updated')):
        previous = None
    if role in CLAUDE_ROLES:
        sid = previous or str(uuid.uuid4())
        # stream-json: olaylar satir satir gelir, panel canli akar.
        # Duz 'json' ciktisi is bitene kadar tek kelime basmiyordu.
        cmd = [executable('claude'), '-p', '--output-format', 'stream-json',
               '--verbose',
               '--permission-mode', 'acceptEdits', '--permission-prompts', 'none',
               # Token saving: roles need no MCP tool schemas in every call.
               '--strict-mcp-config']
        # Token saving: read-and-write-docs roles run on Sonnet.
        # A project .ai/models.json overrides this per role (limit control); no file = old behaviour.
        model = role_model(root, state, role)
        if model:
            cmd += ['--model', model]
        # Only the Developer keeps its session across rework (code context pays off);
        # a resumed Tester/Lead drags the whole previous transcript into every call.
        if role != 'developer':
            previous = None
            sid = str(uuid.uuid4())
        if previous:
            cmd += ['--resume', previous, prompt]
        else:
            cmd += ['--session-id', sid, '--append-system-prompt',
                    team.role_prompt(root, role, ident), prompt]
        return cmd, 'claude', sid
    cmd = [executable('codex'), 'exec']
    # Optional pin from .ai/runner.json "codex_model"; absent = Codex's own default.
    codex_model = runner_config(root).get('codex_model')
    if codex_model:
        cmd += ['-c', 'model=' + json.dumps(str(codex_model))]
    # Verifier only: test runners (e.g. Flutter's) open a loopback VM server that the sandbox
    # blocks. Codex has no loopback-only option, so network access is opened for this role alone.
    if role == 'verifier':
        cmd += ['-c', 'sandbox_mode="workspace-write"',
                '-c', 'sandbox_workspace_write.network_access=true']
        roots = codex_write_roots()
        if roots:
            cmd += ['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps(roots)]
    if previous:
        cmd += ['resume', '--json', previous, prompt]
    else:
        cmd += ['--json', '-s', 'workspace-write', prompt]
    return cmd, 'codex', previous


def engine_model(role, command):
    """(engine, configured model or None) of a role command built by command_for."""
    model = None
    for i, part in enumerate(command[:-1]):
        if part == '--model':
            model = command[i + 1]
        elif part == '-c' and command[i + 1].startswith('model='):
            model = command[i + 1][6:].strip('"')
    return ('claude' if role in CLAUDE_ROLES else 'codex'), model


def run_process(root, ident, role, command, run_id,
                warning_seconds=DEFAULT_TURN_WARNING_MINUTES * 60,
                idle_seconds=DEFAULT_IDLE_TIMEOUT_MINUTES * 60,
                status_seconds=DEFAULT_STATUS_MINUTES * 60):
    log_dir = root / '.ai' / 'runner-logs' / ident
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'{role}-{run_id}.log'
    env = dict(os.environ, AI_TEAM_ROLE=role,
               AI_TEAM_PROJECT=team.read(root/'.ai/project.json')['id'],
               AI_TEAM_TASK=ident, AI_TEAM_RUN_ID=run_id)
    env['ENABLE_CLAUDEAI_MCP_SERVERS'] = 'false'  # claude.ai connectors off for runner roles
    # team.py evidence records the real engine/model, which decides the independence label.
    env['AI_TEAM_ENGINE'], model = engine_model(role, command)
    env.pop('AI_TEAM_MODEL', None)
    if model:
        env['AI_TEAM_MODEL'] = model
    # Project extras from .ai/runner.json: "env" (all roles) and "env_by_role" ({role: {K: V}}).
    rcfg = runner_config(root)
    env.update({str(k): str(v) for k, v in (rcfg.get('env') or {}).items()})
    env.update({str(k): str(v) for k, v in ((rcfg.get('env_by_role') or {}).get(role) or {}).items()})
    thread_id = None
    finished = threading.Event()

    def warn_long_turn():
        # The timer is advisory only: record and display the warning, never terminate the role.
        if finished.is_set():
            return
        record_turn_warning(root, ident, role, run_id, warning_seconds)
        minutes = warning_seconds / 60
        stamp(f'{ident}: UYARI · {role} turu {minutes:g} dakikayı aştı; Lead devir hazırlamalı')
        notify('AI Team uzun rol turu', f'{ident}: {role}, {minutes:g} dakika')

    timer = threading.Timer(warning_seconds, warn_long_turn)
    timer.daemon = True
    timer.start()
    last_output = [time.monotonic()]
    running = {}

    def watch_idle():
        # Unlike the turn warning, silence is not advisory: a role that stops writing is stopped.
        while not finished.wait(min(IDLE_CHECK_SECONDS, idle_seconds)):
            if time.monotonic() - last_output[0] < idle_seconds:
                continue
            process = running.get('process')
            if process is None or process.poll() is not None:
                return
            record_idle_timeout(root, ident, role, run_id, idle_seconds)
            minutes = idle_seconds / 60
            stamp(f'{ident}: DURDURULDU · {role} {minutes:g} dakikadır çıktı üretmedi')
            notify('AI Team rol takıldı', f'{ident}: {role}, {minutes:g} dk sessiz; durduruldu')
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            return

    started = time.monotonic()

    def heartbeat():
        while not finished.wait(status_seconds):
            minutes = (time.monotonic() - started) / 60
            notify(f'AI Team · {ident} · {role}',
                   f'{minutes:.0f} dk çalışıyor · son: {_last_line[0][:150]}')

    threading.Thread(target=heartbeat, daemon=True).start()
    watchdog = threading.Thread(target=watch_idle, daemon=True)
    try:
        with log_path.open('w') as log:
            # The context manager closes the stdout pipe after streamed event handling.
            with subprocess.Popen(command, cwd=root, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  bufsize=1) as process:
                assert process.stdout is not None
                running['process'] = process
                watchdog.start()
                for line in process.stdout:
                    last_output[0] = time.monotonic()
                    log.write(line); log.flush()
                    try:
                        event = json.loads(line)
                        thread_id = event.get('thread_id') or thread_id
                        if event.get('type') == 'thread.started':
                            thread_id = event.get('thread_id') or thread_id
                        if event.get('type') in {'error', 'turn.failed'}:
                            stamp(f'{ident} {role}: ' + line.strip()[:500])
                        else:
                            render_event(f'{ident} {role}', event)
                    except json.JSONDecodeError:
                        pass
                code = process.wait()
    finally:
        # Cancellation prevents a warning from racing with normal role completion.
        finished.set()
        timer.cancel()
    return code, thread_id, log_path


def enabled_tasks(root, only=None):
    result = []
    for state_file in sorted((root / 'tasks').glob('*/state.json')):
        ident = state_file.parent.name
        if only and ident != only:
            continue
        try:
            state = team.read(state_file)
        except (OSError, ValueError) as error:
            # One unreadable task must not stop the runner; it is retried next round.
            warn_once(ident, f'{ident}: ATLANDI · state okunamadı ({error}); sonraki turda denenecek')
            continue
        _warned.pop(ident, None)
        auto = state.get('automation', {})
        if auto.get('enabled'):
            result.append((state_file.parent.name, state))
    return result


LIMIT_MARKERS = ('hit your session limit', 'usage limit', 'hit your limit', 'enotfound',
                 "can't reach the api server", 'overloaded')
LIMIT_FALLBACK = timedelta(minutes=20)
RESET_RE = re.compile(r'resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([\w/+-]+)\))?', re.I)


def limit_retry(log_path, now=None):
    """Retry time when the log tail shows a usage-limit/network failure, else None."""
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            text = f.read().decode(errors='replace')
    except OSError:
        return None
    if not any(m in text.lower().replace('’', "'") for m in LIMIT_MARKERS):
        return None
    now = now or team.utcnow()
    found = RESET_RE.findall(text)
    if found:
        hour, minute, ampm, zone = found[-1]
        try:
            tz = ZoneInfo(zone) if zone else now.astimezone().tzinfo
        except Exception:
            tz = now.astimezone().tzinfo
        local = now.astimezone(tz)
        at = local.replace(hour=int(hour) % 12 + (12 if ampm.lower() == 'pm' else 0),
                           minute=int(minute or 0), second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
        return at + timedelta(minutes=2)
    return now + LIMIT_FALLBACK


def set_waiting_limit(root, ident, role, run_id, retry_at):
    with team.locked(root):
        p, state = load_state(root, ident)
        if role == 'developer':
            state['developer'] = None  # the dead run's lock; the retry claims it again
        auto = state.setdefault('automation', {})
        auto.update(status='WAITING_LIMIT', role=role, run_id=run_id, retry_at=retry_at.isoformat(),
                    message=f'{role} limit/ağ hatası; {retry_at.astimezone():%H:%M} sonra otomatik denenecek',
                    updated=team.now())
        state['updated'] = team.now()
        state['revision'] = state.get('revision', 0) + 1
        team.atomic(p / 'state.json', state)


def waiting_limit(auto):
    if auto.get('status') != 'WAITING_LIMIT':
        return False
    try:
        return team.utcnow() < datetime.fromisoformat(auto['retry_at'])
    except (KeyError, TypeError, ValueError):
        return False


def triage(root, ident, state):
    """Status bookkeeping for one task; returns the role to run now, or None."""
    stage = state['stage']
    auto = state.get('automation', {})
    if stage == 'DONE':
        if auto.get('status') != 'COMPLETED':
            update_runner_state(root, ident, status='COMPLETED', role=None,
                                run_id=None, message='Görev tamamlandı · ' + team.pass_label(state))
            notify('AI Team tamamlandı', f'{ident} · {team.pass_label(state)}')
        return None
    if stage in team.CLOSED_STAGES:  # CLOSED/ABANDONED: closed without the full flow, never run
        return None
    if auto.get('status') in {'BLOCKED', 'PAUSED'} or waiting_limit(auto):
        return None
    unresolved = [g for g in state.get('gates', []) if g.get('status') != 'RESOLVED_BY_LEAD']
    if unresolved:
        if auto.get('status') != 'AWAITING_USER':
            update_runner_state(root, ident, status='AWAITING_USER', role='lead',
                                run_id=None, message='Human gate çözümü bekleniyor')
            notify('AI Team kullanıcı onayı bekliyor', f'{ident}: {unresolved[0].get("kind")}')
        return None
    if stage == 'NEW':
        if auto.get('status') != 'WAITING_FOR_LEAD':
            update_runner_state(root, ident, status='WAITING_FOR_LEAD', role='lead',
                                run_id=None, message='Lead kapsamı ve ilk aşamayı hazırlıyor')
        return None
    role = STAGE_ROLE.get(stage)
    if not role:
        update_runner_state(root, ident, status='BLOCKED', role=None, run_id=None,
                            message='Bilinmeyen aşama: ' + stage)
        notify('AI Team durdu', f'{ident}: bilinmeyen aşama {stage}')
    return role


def run_task(root, ident, state, role, dry_run=False,
             turn_warning_seconds=DEFAULT_TURN_WARNING_MINUTES * 60,
             idle_seconds=DEFAULT_IDLE_TIMEOUT_MINUTES * 60,
             status_seconds=DEFAULT_STATUS_MINUTES * 60):
    stage = state['stage']
    run_id = str(uuid.uuid4())
    prompt = workflow_prompt(root, ident, state, role, run_id)
    command, provider, session_id = command_for(root, ident, state, role, prompt, run_id)
    if dry_run:
        stamp(f'DRY RUN {ident}: {stage} → {role} ({provider})')
        return 'DRY_RUN'
    # Snapshot immediately before launch so a failed replace-style patch is recoverable.
    snapshot_dir = snapshot_task_markdown(root, ident, run_id)
    update_runner_state(root, ident, status='RUNNING', role=role, run_id=run_id,
                        retry_at=None, message=f'{stage} aşaması çalışıyor')
    if session_id:
        save_session(root, ident, role, provider, session_id, run_id)
    stamp(f'{ident}: {role} başladı · {stage}')
    before = team.git_changes(root) if role == 'developer' else None
    try:
        code, discovered_session, log_path = run_process(
            root, ident, role, command, run_id, turn_warning_seconds, idle_seconds,
            status_seconds)
    finally:
        if before is not None:
            record_changes(root, ident, before)
        # A deleted canonical report is restored even when the role process itself fails.
        restored = restore_missing_canonical_reports(root, ident, snapshot_dir)
        if restored:
            names = ', '.join(restored)
            stamp(f'{ident}: UYARI · kayıp rapor snapshot\'tan geri yüklendi: {names}')
            notify('AI Team raporu geri yükledi', f'{ident}: {names}')
    if discovered_session:
        save_session(root, ident, role, provider, discovered_session, run_id)
    _, after = load_state(root, ident)
    if after.get('escalated') and not state.get('escalated'):
        esc = after['escalated']
        stamp(f'{ident}: YÜKSELTİLDİ · fix → small (risk_paths: {", ".join(esc.get("files") or [])}); VERIFY_CODEX eklendi')
    failed = code != 0 or after['stage'] == stage
    retry_at = limit_retry(log_path) if failed else None
    if retry_at:
        set_waiting_limit(root, ident, role, run_id, retry_at)
        notify('AI Team limit bekliyor', f'{ident}: {role}, {retry_at.astimezone():%H:%M}')
        stamp(f'{ident}: BEKLİYOR · {role} limit/ağ hatası · {retry_at.astimezone():%H:%M} sonra yeniden denenecek')
        return 'WAITING_LIMIT'
    if code != 0:
        update_runner_state(root, ident, status='BLOCKED', role=role, run_id=run_id,
                            message=f'{role} çıkış kodu {code}; log: {log_path}')
        notify('AI Team rol hatası', f'{ident}: {role}, çıkış kodu {code}')
        stamp(f'{ident}: DURDU · {role} çıkış kodu {code} · {log_path}')
        return 'BLOCKED'
    if after.get('automation', {}).get('status') == 'BLOCKED':
        # team.py itself blocked the task (rework limit, independence rule); keep its message.
        notify('AI Team kontrol gerekli', f'{ident}: {after["automation"].get("message")}')
        stamp(f'{ident}: DURDU · {after["automation"].get("message")}')
        return 'BLOCKED'
    if after['stage'] == stage:
        update_runner_state(root, ident, status='BLOCKED', role=role, run_id=run_id,
                            message=f'{role} aşamayı ilerletmedi; log: {log_path}')
        notify('AI Team kontrol gerekli', f'{ident}: {role} aşamayı ilerletmedi')
        stamp(f'{ident}: DURDU · {role} aşamayı ilerletmedi · {log_path}')
        return 'BLOCKED'
    done = after['stage'] == 'DONE'
    label = f' · {team.pass_label(after)}' if done else ''
    update_runner_state(root, ident, status='COMPLETED' if done else 'IDLE',
                        role=None, run_id=None,
                        message=f'{stage} → {after["stage"]}{label}')
    stamp(f'{ident}: {stage} → {after["stage"]}{label}')
    if done:
        notify('AI Team tamamlandı', f'{ident}{label}')
    return 'ADVANCED'


def dispatch_once(root, only=None, dry_run=False,
                  turn_warning_seconds=DEFAULT_TURN_WARNING_MINUTES * 60,
                  idle_seconds=DEFAULT_IDLE_TIMEOUT_MINUTES * 60,
                  status_seconds=DEFAULT_STATUS_MINUTES * 60):
    """Run the first runnable task to completion (sequential; used by `once`)."""
    for ident, state in enabled_tasks(root, only):
        role = triage(root, ident, state)
        if role:
            return run_task(root, ident, state, role, dry_run, turn_warning_seconds,
                            idle_seconds, status_seconds)
    return 'IDLE'


# Lead too: its knowledge ack fingerprints the task source, which a parallel Developer would change.
SOURCE_ROLES = {'developer', 'tester', 'verifier', 'lead'}
DEFAULT_RUNNER_CONFIG = {'max_parallel': 3, 'serial_roles': ['tester', 'verifier', 'lead']}


def runner_config(root):
    """Defaults overridden by the project's optional .ai/runner.json."""
    cfg = dict(DEFAULT_RUNNER_CONFIG)
    f = root / '.ai' / 'runner.json'
    if f.is_file():
        cfg.update(team.read(f))
    return cfg


def admissible(running, candidate, cfg):
    """May `candidate` (ident, role, scope) start next to `running` [(ident, role, scope)]?

    One role per task; serial roles (one iOS simulator, one Codex, shared CURRENT_STATE) run
    alone; a Developer and any source-reading role of another task need disjoint scopes,
    because an unscoped task fingerprints the whole repo.
    """
    ident, role, scope = candidate
    if len(running) >= cfg['max_parallel']:
        return False
    for r_ident, r_role, r_scope in running:
        if r_ident == ident or (role == r_role and role in cfg['serial_roles']):
            return False
        if 'developer' in (role, r_role) and {role, r_role} <= SOURCE_ROLES \
                and team.scopes_conflict(scope, r_scope):
            return False
    return True


def run_task_safely(root, ident, state, role, *timing):
    try:
        run_task(root, ident, state, role, False, *timing)
    except Exception as error:  # a failed launch must not kill the other tasks
        update_runner_state(root, ident, status='BLOCKED', role=role, run_id=None,
                            message=f'{role} başlatılamadı: {error}')
        stamp(f'{ident}: DURDU · {role} başlatılamadı: {error}')


def watch_loop(root, only, poll, timing, once_round=False):
    """Start every admissible runnable task in its own thread; poll until interrupted."""
    running = {}  # ident -> (thread, role, scope)
    while True:
        for ident in [i for i, (t, _, _) in running.items() if not t.is_alive()]:
            del running[ident]
        try:
            cfg = runner_config(root)
            tasks = enabled_tasks(root, only)
        except Exception as error:  # e.g. a broken runner.json; keep the loop alive and retry
            warn_once('_round', f'Runner turu atlandı: {error}')
            tasks = []
        for ident, state in tasks:
            if ident in running:
                continue
            try:
                role = triage(root, ident, state)
                if not role:
                    continue
                scope = team.task_scope(team.task(root, ident))
                # A manual/foreign source lock (or an unreadable state: OSError, fail-closed) defers the
                # Developer to a later round instead of launching a run whose claim must fail.
                if role == 'developer' and team.lock_conflict(root, scope, ident):
                    continue
            except Exception as error:  # an I/O problem in one task skips only that task this round
                warn_once(ident, f'{ident}: ATLANDI · {type(error).__name__}: {error}; sonraki turda denenecek')
                continue
            if not admissible([(i, r, s) for i, (_, r, s) in running.items()], (ident, role, scope), cfg):
                continue
            thread = threading.Thread(target=run_task_safely, args=(root, ident, state, role, *timing),
                                      daemon=True)
            running[ident] = (thread, role, scope)
            thread.start()
        if once_round:
            return running
        time.sleep(max(poll, 0.5))


@contextlib.contextmanager
def runner_lock(root):
    path = root / '.ai' / '.runner.lock'
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Bu proje için Team Runner zaten çalışıyor.')
        yield


def agent_unit(root, platform=None):
    """(path, content, load command, unload command) of a per-project user service running `watch`."""
    platform = platform or sys.platform
    pid = re.sub(r'[^A-Za-z0-9_.-]', '-', str(team.read(root/'.ai/project.json').get('id') or root.name))
    log = root/'.ai'/'runner-logs'/'_team_runner.log'
    args = [sys.executable, str(Path(__file__).resolve()), '--project', str(root), 'watch']
    path_env = os.environ.get('PATH', '')
    if platform == 'darwin':
        label = f'com.ai-orchestra.runner.{pid}'
        path = Path.home()/'Library'/'LaunchAgents'/f'{label}.plist'
        body = plistlib.dumps({'Label': label, 'ProgramArguments': args, 'RunAtLoad': True,
                               'KeepAlive': True, 'WorkingDirectory': str(root),
                               'StandardOutPath': str(log), 'StandardErrorPath': str(log),
                               'EnvironmentVariables': {'PATH': path_env}}).decode()
        quoted = shlex.quote(str(path))
        return path, body, f'launchctl bootstrap gui/$(id -u) {quoted}', f'launchctl bootout gui/$(id -u) {quoted}'
    name = f'ai-orchestra-runner-{pid}.service'
    path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'systemd'/'user'/name
    body = (f'[Unit]\nDescription=ai-orchestra runner ({pid})\n\n[Service]\n'
            f'ExecStart={shlex.join(args)}\nWorkingDirectory={root}\nEnvironment="PATH={path_env}"\n'
            f'Restart=always\nStandardOutput=append:{log}\nStandardError=append:{log}\n\n'
            '[Install]\nWantedBy=default.target\n')
    return path, body, f'systemctl --user daemon-reload && systemctl --user enable --now {name}', \
        f'systemctl --user disable --now {name}'


def install_agent(root, dry_run=False, platform=None):
    """Write the service file; the user runs the printed load command (never run here)."""
    path, body, load, _ = agent_unit(root, platform)
    if dry_run:
        return f'DRY-RUN, yazılmadı: {path}\n{body}\nYüklemek için:\n  {load}'
    path.parent.mkdir(parents=True, exist_ok=True)
    (root/'.ai'/'runner-logs').mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return f'Yazıldı: {path}\nYüklemek için kendin çalıştır:\n  {load}'


def uninstall_agent(root, apply=False, platform=None):
    path, _, _, unload = agent_unit(root, platform)
    out = f'Önce durdur (kendin çalıştır):\n  {unload}'
    if not apply:
        return out + f'\nDRY-RUN: {path} silinmedi; silmek için --apply.'
    if path.exists():
        path.unlink()
        return out + f'\nSilindi: {path}'
    return out + f'\nDosya yok: {path}'


def show_status(root):
    rows = []
    for state_file in sorted((root/'tasks').glob('*/state.json')):
        state = team.read(state_file); auto = state.get('automation', {})
        rows.append({'task': state_file.parent.name, 'stage': state['stage'],
                     'enabled': bool(auto.get('enabled')), 'runner': auto.get('status', 'LEGACY'),
                     'role': auto.get('role'), 'message': auto.get('message')})
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default=os.getcwd())
    parser.add_argument('--task')
    # CLI configuration keeps the default safe while allowing per-run tuning.
    parser.add_argument('--turn-warning-minutes', type=float,
                        default=DEFAULT_TURN_WARNING_MINUTES)
    parser.add_argument('--idle-timeout-minutes', type=float,
                        default=DEFAULT_IDLE_TIMEOUT_MINUTES)
    parser.add_argument('--status-minutes', type=float, default=DEFAULT_STATUS_MINUTES)
    sub = parser.add_subparsers(dest='command', required=True)
    once = sub.add_parser('once'); once.add_argument('--dry-run', action='store_true')
    watch = sub.add_parser('watch'); watch.add_argument('--poll', type=float, default=2.0)
    sub.add_parser('status')
    q = sub.add_parser('install-agent'); q.add_argument('--project', default=argparse.SUPPRESS)
    q.add_argument('--dry-run', action='store_true')
    q = sub.add_parser('uninstall-agent'); q.add_argument('--project', default=argparse.SUPPRESS)
    q.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.turn_warning_minutes <= 0:
        parser.error('--turn-warning-minutes must be greater than zero')
    if args.idle_timeout_minutes <= 0:
        parser.error('--idle-timeout-minutes must be greater than zero')
    root = team.project(args.project)
    if args.command == 'status':
        show_status(root); return
    if args.command == 'install-agent':
        print(install_agent(root, args.dry_run)); return
    if args.command == 'uninstall-agent':
        print(uninstall_agent(root, args.apply)); return
    with runner_lock(root):
        if args.command == 'once':
            dispatch_once(root, args.task, args.dry_run,
                          args.turn_warning_minutes * 60,
                          args.idle_timeout_minutes * 60); return
        stamp(f'Team Runner hazır · {root}')
        stamp('Yalnız --auto görevler çalışır; NEW aşamasında Lead beklenir. Ctrl-C ile durdurabilirsiniz.')
        watch_loop(root, args.task, args.poll, (args.turn_warning_minutes * 60,
                   args.idle_timeout_minutes * 60, args.status_minutes * 60))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        stamp('Team Runner durduruldu.')
    except (RuntimeError, ValueError, OSError) as error:
        print('STOP:', error, file=sys.stderr)
        sys.exit(2)
