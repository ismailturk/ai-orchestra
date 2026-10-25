#!/usr/bin/env python3
"""Visible, fail-closed role orchestrator for onboarded AI-Team projects."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import quota
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


# Tool-layer refusal for Claude roles (deny rules win over any project allow list). Prompts are not a
# security boundary; these commands need a human whatever the role is told.
DENY_TOOLS = ['Bash(git push:*)', 'Bash(git merge:*)', 'Bash(git rebase:*)', 'Bash(git reset --hard:*)',
              'Bash(git clean:*)', 'Bash(git branch -D:*)', 'Bash(git checkout main:*)', 'Bash(git checkout master:*)',
              'Bash(git switch main:*)', 'Bash(git switch master:*)', 'Bash(gh pr merge:*)', 'Bash(gh release:*)',
              'Bash(dotnet ef database:*)', 'Bash(dotnet ef migrations remove:*)', 'Bash(psql:*)', 'Bash(mysql:*)',
              'Bash(sqlcmd:*)', 'Bash(flutter pub publish:*)', 'Bash(dart pub publish:*)', 'Bash(npm publish:*)',
              'Bash(firebase deploy:*)', 'Bash(fastlane:*)', 'Bash(sudo:*)', 'Bash(rm -rf:*)']
# Both engines: every network push URL is rewritten to an unknown scheme, so `git push` fails in git itself.
PUSH_BLOCK_PREFIXES = ('https://', 'http://', 'ssh://', 'git://', 'git@')


def push_block_env(env):
    """Add GIT_CONFIG_* entries (git >= 2.31) that make pushes from a role process fail."""
    n = int(env.get('GIT_CONFIG_COUNT') or 0)
    for i, prefix in enumerate(PUSH_BLOCK_PREFIXES, n):
        env[f'GIT_CONFIG_KEY_{i}'] = 'url.ai-orchestra-push-blocked://.pushInsteadOf'
        env[f'GIT_CONFIG_VALUE_{i}'] = prefix
    env['GIT_CONFIG_COUNT'] = str(n + len(PUSH_BLOCK_PREFIXES))
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env


_last_line = ['']
_warned = {}


def stamp(message):
    _last_line[0] = message
    print(time.strftime('[%H:%M:%S]'), message, flush=True)


def warn_once(key, message, token=None):
    """Log a recurring problem once (per distinct token, default the message) instead of every poll."""
    token = message if token is None else token
    if _warned.get(key) != token:
        _warned[key] = token
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


LIVE = set()  # role processes of this runner; stopped with the runner (they no longer share its group)
# Quota telemetry per run_id, filled by run_process from the role's own stream ({'first', 'last', 'reset'}).
TELEMETRY = {}
CAFFEINATE = '/usr/bin/caffeinate'


def observe_rate_limit(run_id, event, seen):
    """Feed a Claude `rate_limit_event` to the shared quota snapshot (throttled) and this run's telemetry."""
    wins, status = quota.claude_windows(event)
    if not wins:
        return
    t = TELEMETRY.setdefault(run_id, {})
    t.setdefault('first', wins)
    t['last'] = wins
    if status == 'rejected':
        t['reset'] = (event.get('rate_limit_info') or {}).get('resetsAt')
    if wins != seen[0] or status == 'rejected' or time.monotonic() - seen[1] > 60:
        try:
            quota.observe('claude', wins, status)
        except Exception as error:  # telemetry must never stop a role
            warn_once('_quota', f'Kota kaydı yazılamadı: {error}')
        seen[:] = [wins, time.monotonic()]


def stop_process(process):
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def record_stop(root, ident, role, run_id, kind, message):
    with team.locked(root):
        p, state = load_state(root, ident)
        auto = state.setdefault('automation', {})
        auto.setdefault('warnings', []).append({'kind': kind, 'role': role, 'run_id': run_id, 'created': team.now()})
        auto.update(message=message, updated=team.now())
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


def record_changes(root, ident, before, src=None):
    """Add the files this Developer run touched to the task's changed_files (`src`: the tree `before` came from)."""
    after = team.git_changes(src or root)
    with team.locked(root):
        p, state = load_state(root, ident)
        outside = team.add_changed(p, state, before, after)
        team.atomic(p / 'state.json', state)
    if outside:
        stamp(f'{ident}: UYARI · kapsam dışı değişiklik: {", ".join(outside[:10])}')


def telegram_config():
    """~/.config/ai-team/telegram.json {"enabled": true, "bot_token": "...", "chat_id": "..."}; opt-in.
    Refused unless the file is private (0600): it holds a bot token."""
    f = team.config_dir() / 'telegram.json'
    try:
        if f.stat().st_mode & 0o077:
            warn_once('_telegram', 'Telegram kapalı: telegram.json izni 0600 olmalı (chmod 600)')
            return None
        cfg = team.read(f)
    except (OSError, ValueError):
        return None
    return cfg if cfg.get('enabled') and cfg.get('bot_token') and cfg.get('chat_id') else None


def send_telegram(title, body):
    """Outbound status only (no commands are read back). Runs in a thread; failures never reach the role."""
    cfg = telegram_config()
    if not cfg or title.startswith('AI Team · '):  # periodic heartbeats stay local
        return None
    import urllib.parse
    import urllib.request
    import intel
    data = urllib.parse.urlencode({'chat_id': cfg['chat_id'], 'disable_web_page_preview': 'true',
                                   'text': intel.redact(f'{title}\n{body}')[:900]}).encode()

    def go():
        try:
            urllib.request.urlopen(f'https://api.telegram.org/bot{cfg["bot_token"]}/sendMessage', data, timeout=8).close()
        except Exception:
            warn_once('_telegram_send', 'Telegram bildirimi gönderilemedi')  # never the URL: it holds the token
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def notify(title, body):
    send_telegram(title, body)
    if not os.environ.get('CMUX_WORKSPACE_ID') or not Path(CMUX).is_file():
        return
    subprocess.run([CMUX, 'notify', '--title', title, '--body', body],
                   text=True, capture_output=True)


def state_digest(state, role=None):
    """The state.json fields a role needs, inline: saves every role re-reading (and carrying) the whole file."""
    ev = lambda k: (state.get(k) or {}).get('status') or (state.get(k) or {}).get('result') or '-'
    gates = [f"{g.get('kind')}:{g.get('status')}" for g in state.get('gates') or []]
    return (f"Durum özeti (state.json'u okuma; gerekirse `team status {state.get('task')}`): profil {state.get('profile')}, "
            f"yol {' → '.join(state.get('stage_path') or [])}, rework {state.get('rework_count', 0)}"
            + (f" ({state['rework_reason']})" if state.get('rework_reason') else '')
            + f", Claude test {ev('claude_test')}, Codex doğrulama {ev('codex_verify')}, değişen dosya {len(state.get('changed_files') or [])}"
            + (f", gate {', '.join(gates)}" if gates else '') + '.'
            # Tester/Verifier start from the change set instead of rediscovering it with git/grep calls.
            + (f"\nDeğişen dosyalar: {', '.join(changed[:25])}" + (f' (+{len(changed) - 25})' if len(changed) > 25 else '')
               if role in ('tester', 'verifier') and (changed := state.get('changed_files') or []) else ''))


def recent_findings(root, ident, limit=6):
    """Latest distinct rework reasons of OTHER tasks (what Tester/Verifier caught): the Developer avoids and the
    Tester checks the same kind first, so it is caught before a Codex round. Free: read from state files."""
    rows = []
    for f in (root / 'tasks').glob('*/state.json'):
        if f.parent.name == ident:
            continue
        try:
            s = team.read(f)
        except (OSError, ValueError):
            continue
        if s.get('rework_reason'):
            rows.append((s.get('updated') or '', f.parent.name, ' '.join(str(s['rework_reason']).split())[:170]))
    seen, out = set(), []
    for _, other, reason in sorted(rows, reverse=True):
        if reason[:60] not in seen:
            seen.add(reason[:60]); out.append(f'- {other}: {reason}')
    return out[:limit]


def workflow_prompt(root, ident, state, role, run_id):
    path = state.get('stage_path') or team.WORKFLOWS[state.get('profile', 'full')]
    stage = state['stage']
    next_stage = path[path.index(stage) + 1] if path.index(stage) + 1 < len(path) else None
    tool = Path(team.__file__).resolve()
    base = ['python3', str(tool), '--project', str(root)]

    def command(*parts):
        import shlex
        return shlex.join([*base, *parts])

    worktree = state.get('worktree')
    where = (f"""
Kaynak kodu İZOLE git worktree'de: {worktree['src']} (dal {worktree['branch']}; çalışma dizinin). Kodu yalnız orada düzenle.
Koordinasyon kökü {root}: tasks/{ident}/ belgeleri, .ai/ ve roles/ dosyalarını bu kökte MUTLAK yolla oku/yaz.
""" if worktree else '')
    common = f"""
Bu çalışma Team Runner tarafından otomatik başlatıldı.
Görev: {ident}. Aktif aşama: {stage}. Çalışma kimliği: {run_id}.
Yalnız bu aşamayı tamamla. Human gate gerektiren hiçbir işlemi çalıştırma; gate kaydı oluşturup dur.
Başarı komutlarını ancak ilgili belge eksiksiz yazıldıktan ve doğrulandıktan sonra çalıştır.
Komut başarısız olursa aşamayı elle atlama. Kullanıcıya soru sorma; engeli Lead mesajına ve rapora yazıp çık.
git push/merge/rebase, DB komutları ve yayınlama araç katmanında engellidir; deneme.
Kullanıcı Command Center'dan not ekleyebilir: tasks/{ident}/TASK.md içindeki "## Kullanıcı yanıtı" bölümlerini oku.
Notu bu aşamanın sınırında uygula; kapsamı/kabulü değiştiriyorsa uygulama, Lead'e mesaj bırak. Not human gate onayı değildir.
Komutları çalışma dizininde göreli yolla çalıştır; `cd`, `X=...;` değişkeni, `for` döngüsü, heredoc/`>` yönlendirmesi ve
`sed -i` izin listesine uymaz, otomatik reddedilir ve tur boşa gider; araçları (dotnet, flutter, npm…) mutlak yolla değil
adıyla çağır. Dosyayı Write/Edit ile yaz.
""".strip() + where + '\n' + state_digest(state, role)
    resume = resume_marker(state, role)
    if resume:
        why = 'kullanım limiti' if resume.get('kind') == 'limit' else 'ağ hatası'
        common = (f"DEVAM TURU: önceki {role} turu {why} yüzünden yarıda kaldı; bu aynı oturumun devamı. "
                  f"Çalışma kimliğin artık {run_id}: kilit ve kanıt komutlarında bunu kullan"
                  + ('; kaynak kilidi bırakıldı, düzenlemeden önce yeniden al' if role == 'developer' else '')
                  + ". Yarım kalan son adımın sonucunu doğrula, yapılmış işi tekrarlama, kaldığın yerden bitir.\n" + common)

    common += (f"\nMesaj: {command('message', ident, '--from', role, '--to', '<rol>', '--body', '<metin>')}"
               f"\nRework: {command('rework', ident, '--reason', '<neden>')}  (komut sözdizimi bu kadar; --help gerekmez)")

    findings = recent_findings(root, ident) if role in ('developer', 'tester') else []
    if findings:
        common += ('\nBu projede bağımsız kontrollerin son yakaladığı hatalar (bağlam; aynı türü '
                   + ('tekrarlama' if role == 'developer' else 'önce kontrol et') + '):\n' + '\n'.join(findings))

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
İlerletmeden önce derle ve değişikliğe dokunan mevcut testleri (mimari testler dahil) yalnız hataları gösterecek biçimde koş;
kırmızıysa düzelt. Derlenmeyen ya da bilinen kırmızı testle ilerleyen iş Tester ve Codex turunu boşa harcar.
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
    elif role == 'lead' and stage == 'NEW':
        intake = state.get('intake') or {}
        skills = '; '.join(f"{s['name']} — {s['why']}" for s in state.get('skills') or []) or 'yok'
        protocol = f"""
Bu bir GÖREV GİRİŞİ turudur. Kullanıcının isteği tasks/{ident}/TASK.md içinde "## Kullanıcı isteği" bölümünde;
varsa "## Kullanıcı yanıtı" bölümleri önceki sorularına cevaptır. Üretim kodu yazma, yalnız oku.
1. Proje hafızasını (.ai/PROJECT.md, .ai/CURRENT_STATE.md) ve ilgili kodu grep/Glob ile incele.
2. TASK.md'yi doldur: amaç, kapsam, kabul kriterleri, riskler ve en dar `## Dosyalar` globları. Kullanıcı isteği/yanıt bölümlerini silme.
   İş birden çok bağımsız dilimse (S1..Sn) kapsamı ilk dilimle sınırla, kalanları "## Sonraki dilimler" altında listele:
   tek turda bitmeyen görev yarıda kalır ve tekrar okunur. Başka bir göreve bağlıysa "## Bağımlılıklar" altına o görevin ID'sini yaz.
3. Runner'ın kural tabanlı profil önerisi: {state.get('profile')} ({(state.get('classification') or {}).get('reason', '-')}).
   Gerekirse yalnız şu komutla değiştir (auth/güvenlik/ödeme/DB işi fix olamaz; DB/deploy/push için gate kaydı aç):
   {command('reclassify', ident, '<fix|small|ui|full|risky>', '--reason', '<neden>')}
4. Önerilen skill'ler (yalnız ilgiliyse kullan, TASK.md'de belirt): {skills}
5. İstek belirsizse ya da kullanıcı kararı gerekiyorsa TASK.md sonuna "## Lead sorusu" başlığıyla kısa soruyu yaz ve aşamayı İLERLETME;
   runner görevi kullanıcı yanıtına park eder.
6. Hazırsa state.json'daki stage_path'in ikinci aşamasına ilerlet (profil değişmediyse):
   {command('advance', ident, next_stage)}
""" + ('' if intake.get('text') else '\nNot: görev elle açıldı; kullanıcı isteği TASK.md başlığındadır.\n')
    elif role == 'lead' and STAGE_ROLE.get(stage) != 'lead':
        turn = (state.get('automation') or {}).get('lead_turn') or {}
        owner = STAGE_ROLE.get(stage)
        protocol = f"""
Bu bir LEAD KARAR turudur: aşama {stage}, aşamanın rolü {owner}. Neden: {turn.get('reason') or '-'}.
Kısa tut. Üretim kodu yazma, aşamayı İLERLETME; karar sonrası runner {owner} rolünü yeniden çalıştırır.
1. tasks/{ident}/ belgelerini (özellikle {owner} raporu ve "## Kullanıcı yanıtı" bölümleri), .ai/messages/lead/ mesajlarını{(' ve ' + turn['log']) if turn.get('log') else ''} oku.
2. {owner} rolünün beklediği kararı ver: TASK.md sonuna "## Lead kararı" başlığıyla kısa, uygulanabilir kararı yaz ve role mesaj bırak:
   {command('message', ident, '--from', 'lead', '--to', owner or 'lead', '--body', '<karar>')}
3. İş yanlış yöne gittiyse uygun komutu kullan (rework/reclassify); Developer'ın düzeltmesi gerekiyorsa rework doğru yoldur.
4. Karar kullanıcıya aitse (kapsam/kabul değişikliği, iş kuralı belirsizliği) ya da rol insan kanıtı istiyorsa (cihaz ölçümü, kullanıcı seçimi) karar verme — kendi kararınla rol aynı nedenle yine durur: TASK.md sonuna "## Lead sorusu" başlığıyla kısa soruyu yaz; runner kullanıcıya sorar.
Human gate (DB, deploy, push, gerçek veri/hesap) onayını sen veremezsin; gate kaydı kullanıcıdadır.
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
    head = '' if resume else team.role_prompt(root, role, ident) + '\n\n'
    return head + common + '\n' + protocol.strip()


def lead_question(root, ident):
    """Text under the last "## Lead sorusu" heading of TASK.md while it is still open, else None.
    Open = the heading is not marked YANITLANDI and no "## Kullanıcı yanıtı" follows it."""
    f = team.task(root, ident) / 'TASK.md'
    parts = re.split(r'^## Lead sorusu([^\n]*)\n', f.read_text() if f.is_file() else '', flags=re.M)
    if len(parts) < 3 or 'YANITLANDI' in parts[-2].upper() or re.search(r'^## Kullanıcı yanıtı', parts[-1], re.M):
        return None
    return re.split(r'^## ', parts[-1], maxsplit=1, flags=re.M)[0].strip() or None


def headless_path():
    """PATH without terminal-integration shims (cmux): roles and services run the real CLI, not a wrapper
    that adds its own hooks and computer-use MCP server to every `claude -p`/`codex exec` session."""
    return os.pathsep.join(p for p in os.environ.get('PATH', '').split(os.pathsep) if '/cmux-cli-shims' not in p)


def executable(name):
    found = shutil.which(name, path=headless_path() or None)
    if found:
        return found
    if name == 'codex':
        fallback = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
        if fallback.is_file():
            return str(fallback)
    raise ValueError(name + ' executable not found')


RESUME_MAX_AGE = timedelta(minutes=55)
# A run cut by a usage limit or the network continues its own session this many times, then starts fresh.
RESUME_ATTEMPTS = 3


def resume_marker(state, role):
    """The automation 'resume' marker when it belongs to this role, stage, rework round and engine."""
    r = (state.get('automation') or {}).get('resume') or {}
    provider = 'claude' if role in CLAUDE_ROLES else 'codex'
    if (r.get('role') == role and r.get('stage') == state.get('stage') and r.get('provider') == provider
            and r.get('rework') == state.get('rework_count', 0) and r.get('session')
            and (r.get('attempt') or 1) <= RESUME_ATTEMPTS):
        return r
    return None


def did_work(log_path):
    """True when the cut run got past its first request (a tool ran), so its transcript is worth resuming."""
    try:
        text = Path(log_path).read_text(errors='replace')
    except OSError:
        return False
    return '"tool_use"' in text or '"command_execution"' in text
# Automatic Lead decision turns per stage (and rework round) before the task is parked for the user.
LEAD_HANDOFFS = 2


def session_stale(updated):
    try:
        last = datetime.fromisoformat(updated)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - last > RESUME_MAX_AGE


def role_model(root, state, role):
    f = root / '.ai' / 'models.json'
    models = team.read(f) if f.is_file() else {}
    # Complex/critical requests get the strongest Lead for scoping; "lead_complex" overrides.
    if role == 'lead' and state.get('stage') == 'NEW' and state.get('profile') in {'full', 'risky'}:
        return models.get('lead_complex') or 'opus'
    if not f.is_file():
        return 'sonnet' if role in {'analyst', 'designer', 'lead'} else None
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


def verifier_sandbox():
    """Codex `-c` flags of the verifier's sandbox: workspace-write with network and the tooling dirs writable."""
    flags = ['-c', 'sandbox_mode="workspace-write"', '-c', 'sandbox_workspace_write.network_access=true']
    roots = codex_write_roots()
    return flags + (['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps(roots)] if roots else [])


def sandbox_probe(root, state):
    """(label, argv, cwd) starting the project's toolchain inside the verifier's own sandbox, or None.
    Several verifier runs stopped because Flutter could not write its SDK cache in the sandbox; `codex sandbox`
    shows that in <1 s without a model call, so the task waits instead of burning a run."""
    src = Path(state['worktree']['src']) if (state or {}).get('worktree') else root
    tool = (['flutter', '--version'] if (src / 'pubspec.yaml').is_file() else
            ['dotnet', '--version'] if any(src.glob('*.sln*')) or any(src.glob('*.csproj')) else None)
    if not tool:
        return None
    try:
        codex = executable('codex')
    except ValueError:
        return None
    return f"Codex sandbox'ında `{' '.join(tool)}`", [codex, 'sandbox', *verifier_sandbox(), '--', *tool], src


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
    # A run cut by a limit/network failure continues its own transcript (its work is in there), even
    # past the cache TTL and for roles that otherwise start fresh: redoing the work costs more.
    resume = resume_marker(state, role)
    tool = Path(team.__file__).resolve()
    if role in CLAUDE_ROLES:
        sid = previous or str(uuid.uuid4())
        # stream-json: olaylar satir satir gelir, panel canli akar.
        # Duz 'json' ciktisi is bitene kadar tek kelime basmiyordu.
        cmd = [executable('claude'), '-p', '--output-format', 'stream-json',
               '--verbose',
               '--permission-mode', 'acceptEdits', '--permission-prompts', 'none',
               # Token saving: roles need no MCP tool schemas in every call.
               '--strict-mcp-config',
               # Token saving: cwd/git status move to the first message, so the system prompt (and its
               # cache) is the same in every task worktree.
               '--exclude-dynamic-system-prompt-sections',
               # The runner's own state tool, whatever the project allow list says (an old path there
               # auto-denied every claim/advance and the role stopped without advancing).
               '--allowedTools', f'Bash(python3 {tool} *)',
               '--disallowedTools', *DENY_TOOLS]
        if state.get('worktree'):
            cmd += ['--add-dir', str(root)]  # cwd is the worktree; task docs stay in the project
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
        if resume:
            previous = sid = resume['session']
        if previous:
            cmd += ['--resume', previous, prompt]
        else:
            cmd += ['--session-id', sid, '--append-system-prompt',
                    team.role_prompt(root, role, ident), prompt]
        return cmd, 'claude', sid
    cmd = [executable('codex'), 'exec']
    if state.get('worktree'):
        cmd += ['-C', state['worktree']['src'], '--add-dir', str(root)]
    # Optional pin from .ai/runner.json "codex_model"; absent = Codex's own default.
    cfg = runner_config(root)
    if cfg.get('codex_model'):
        cmd += ['-c', 'model=' + json.dumps(str(cfg['codex_model']))]
    # Optional extra flags, e.g. reasoning effort or a verifier without plugins (README: Kota).
    extra = cfg.get('codex_args') or []
    cmd += [str(a) for a in extra] if isinstance(extra, list) else shlex.split(str(extra))
    # Verifier only: test runners (e.g. Flutter's) open a loopback VM server that the sandbox
    # blocks. Codex has no loopback-only option, so network access is opened for this role alone.
    if role == 'verifier':
        cmd += verifier_sandbox()
    # Each verification round is a fresh thread (a resumed one drags every earlier round into each call);
    # only a run cut by a limit/network failure continues its own thread.
    previous = resume['session'] if resume else None
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
                status_seconds=DEFAULT_STATUS_MINUTES * 60, cwd=None):
    log_dir = root / '.ai' / 'runner-logs' / ident
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'{role}-{run_id}.log'
    env = dict(os.environ, AI_TEAM_ROLE=role,
               AI_TEAM_PROJECT=team.read(root/'.ai/project.json')['id'],
               AI_TEAM_TASK=ident, AI_TEAM_RUN_ID=run_id, AI_TEAM_ROOT=str(root))
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
    push_block_env(env)
    hard_seconds = (team.limits(root).get('role_minutes') or 0) * 60
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
        # The same loop honours a user cancel request and the optional hard role time limit.
        while not finished.wait(min(IDLE_CHECK_SECONDS, idle_seconds)):
            process = running.get('process')
            if process is None or process.poll() is not None:
                return
            try:
                auto = team.read(team.task(root, ident) / 'state.json').get('automation') or {}
            except (OSError, ValueError):
                auto = {}
            if auto.get('cancel_requested') == run_id:
                stamp(f'{ident}: İPTAL · {role} kullanıcı isteğiyle durduruluyor')
            elif hard_seconds and time.monotonic() - started >= hard_seconds:
                record_stop(root, ident, role, run_id, 'ROLE_TIMEOUT',
                            f'{role} {hard_seconds / 60:g} dk rol süre sınırını aştı; runner durdurdu')
                stamp(f'{ident}: DURDURULDU · {role} rol süre sınırı ({hard_seconds / 60:g} dk)')
            elif time.monotonic() - last_output[0] < idle_seconds:
                continue
            else:
                record_idle_timeout(root, ident, role, run_id, idle_seconds)
                minutes = idle_seconds / 60
                stamp(f'{ident}: DURDURULDU · {role} {minutes:g} dakikadır çıktı üretmedi')
                notify('AI Team rol takıldı', f'{ident}: {role}, {minutes:g} dk sessiz; durduruldu')
            stop_process(process)
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
            # Own process group: a stop reaches the role's children too (a shell's test
            # runner kept the output pipe open after the role itself was gone).
            with subprocess.Popen(command, cwd=cwd or root, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  bufsize=1, start_new_session=True) as process:
                assert process.stdout is not None
                running['process'] = process
                LIVE.add(process)
                watchdog.start()
                if Path(CAFFEINATE).exists():  # idle sleep would freeze the role; the assertion ends with it
                    awake = subprocess.Popen([CAFFEINATE, '-i', '-s', '-w', str(process.pid)],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    threading.Thread(target=awake.wait, daemon=True).start()
                seen = [None, 0.0]
                for line in process.stdout:
                    last_output[0] = time.monotonic()
                    log.write(line); log.flush()
                    try:
                        event = json.loads(line)
                        if event.get('type') == 'rate_limit_event':
                            observe_rate_limit(run_id, event, seen)
                            continue
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
                LIVE.discard(process)
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


USAGE_MARKERS = ('hit your session limit', 'hit your weekly limit', 'usage limit', 'hit your limit',
                 'out of credits', 'spend cap')  # the last two: Codex workspace plans
NETWORK_MARKERS = ('enotfound', "can't reach the api server", 'overloaded', 'stream disconnected', 'econnreset')
LIMIT_FALLBACK = timedelta(minutes=20)
# Network blips (e.g. the Mac woke from lid-closed sleep) retry fast; repeated ones back off.
NETWORK_BACKOFF = (2, 5, 10, 20)
RESET_RE = re.compile(r'(?:resets|try again at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([\w/+-]+)\))?', re.I)
# A reset on another day: Claude "resets Oct 25 at 5pm", Codex "try again at Oct 30th, 2026 4:31 PM".
WEEKLY_RE = re.compile(r'(?:resets|try again at)\s+([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?'
                       r'(?:,?\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([\w/+-]+)\))?', re.I)
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')


def failure_text(log_path):
    """The CLI's own error output of a run: final error result, error/turn.failed events and non-JSON lines.
    Tool output inside events is never scanned (a test printing "overloaded" is not a network failure)."""
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            lines = f.read().decode(errors='replace').splitlines()
    except OSError:
        return '', False
    texts, status429 = [], False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            texts.append(line)
            continue
        if not isinstance(e, dict):
            continue
        if e.get('type') == 'result' and e.get('is_error'):
            texts.append(str(e.get('result') or ''))
            status429 = status429 or e.get('api_error_status') == 429
        elif e.get('type') == 'error':
            texts.append(str(e.get('message') or ''))
        elif e.get('type') == 'turn.failed':
            texts.append(str((e.get('error') or {}).get('message') or ''))
    return '\n'.join(texts).replace('\u2019', "'"), status429


def reset_time(text, now):
    """Reset moment named in a limit message ("resets 9:10pm (Europe/Istanbul)", "try again at 4:31 PM",
    "resets Oct 25 at 5pm"), else None."""
    def zone(name):
        try:
            return ZoneInfo(name) if name else now.astimezone().tzinfo
        except Exception:
            return now.astimezone().tzinfo
    found = WEEKLY_RE.findall(text)
    if found and found[-1][0].lower()[:3] in MONTHS:
        mon, day, year, hour, minute, ampm, name = found[-1]
        local = now.astimezone(zone(name))
        try:
            at = local.replace(year=int(year or local.year), month=MONTHS.index(mon.lower()[:3]) + 1, day=int(day),
                               hour=int(hour) % 12 + (12 if ampm.lower() == 'pm' else 0),
                               minute=int(minute or 0), second=0, microsecond=0)
        except ValueError:
            return None
        return at if at > local or year else at.replace(year=at.year + 1)
    found = RESET_RE.findall(text)
    if found:
        hour, minute, ampm, name = found[-1]
        local = now.astimezone(zone(name))
        at = local.replace(hour=int(hour) % 12 + (12 if ampm.lower() == 'pm' else 0),
                           minute=int(minute or 0), second=0, microsecond=0)
        return at if at > local else at + timedelta(days=1)
    return None


def interruption(log_path, now=None, attempt=1, reset_epoch=None):
    """(kind, retry time) when a run died of a usage limit ('limit') or the network ('network'), else (None, None).
    `reset_epoch` is the exact reset the engine's own telemetry reported for a rejected window."""
    text, status429 = failure_text(log_path)
    low = text.lower()
    now = now or team.utcnow()
    if status429 or any(m in low for m in USAGE_MARKERS) or reset_epoch:
        if reset_epoch and reset_epoch > now.timestamp():
            at = datetime.fromtimestamp(reset_epoch, timezone.utc)
        else:
            at = reset_time(text, now) or now + LIMIT_FALLBACK
        return 'limit', at + timedelta(minutes=2)
    if any(m in low for m in NETWORK_MARKERS):
        return 'network', now + timedelta(minutes=NETWORK_BACKOFF[min(attempt, len(NETWORK_BACKOFF)) - 1])
    return None, None


def limit_retry(log_path, now=None):
    """Retry time when the log shows a usage-limit/network failure, else None (first attempt)."""
    return interruption(log_path, now)[1]


def set_waiting_limit(root, ident, role, run_id, retry_at, resume=None, kind='limit'):
    with team.locked(root):
        p, state = load_state(root, ident)
        if role == 'developer':
            state['developer'] = None  # the dead run's lock; the retry claims it again
        auto = state.setdefault('automation', {})
        why = 'kullanım limiti' if kind == 'limit' else 'ağ hatası'
        auto.update(status='WAITING_LIMIT', role=role, run_id=run_id, retry_at=retry_at.isoformat(), resume=resume,
                    message=f'{role} {why}; {retry_at.astimezone():%H:%M} sonra '
                            + ('kaldığı yerden sürecek' if (resume or {}).get('session') else 'otomatik denenecek'),
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


DEP_ID = re.compile(r'\b[A-Za-z]{1,8}-?\d[\w-]*')


def pending_deps(root, ident):
    """Existing, not yet closed tasks listed under TASK.md "## Bağımlılık(lar)" (one id per line or comma-separated)."""
    f = team.task(root, ident) / 'TASK.md'
    m = re.search(r'^## Bağımlılık[^\n]*\n(.*?)(?=^## |\Z)', f.read_text() if f.is_file() else '', re.S | re.M)
    out = []
    for dep in DEP_ID.findall(m.group(1) if m else ''):
        sf = root / 'tasks' / dep / 'state.json'
        if dep != ident and dep not in out and sf.is_file():
            try:
                if team.read(sf).get('stage') not in team.CLOSED_STAGES:
                    out.append(dep)
            except (OSError, ValueError):
                out.append(dep)  # unreadable: fail closed
    return out


ROLE_OUTPUTS = {'state.json', 'TEST_CLAUDE.md', 'VERIFY_CODEX.md', 'FINAL_REPORT.md'}
_inputs = {}  # ident -> (checked monotonic, docs mtimes, digest)


def stage_inputs(root, ident, state, max_age=0.0):
    """Digest of what could unblock a stopped tester/verifier: the task's source, its task documents (TASK,
    DECISIONS, Lead notes; user replies land in TASK.md) and the project DECISIONS.md, not the roles' own reports.
    A cached digest is reused for max_age seconds while no document changed (source hashing is the costly part)."""
    p = team.task(root, ident)
    docs = [f for f in sorted(p.iterdir()) + [root / '.ai' / 'DECISIONS.md'] if f.is_file() and f.name not in ROLE_OUTPUTS]
    stamp_ = [(f.name, f.stat().st_mtime_ns) for f in docs]
    hit = _inputs.get(ident)
    if hit and hit[1] == stamp_ and time.monotonic() - hit[0] < max_age:
        return hit[2]
    h = hashlib.sha256(team.snapshot(root, scope=team.task_scope(p), sources=team.task_sources(root, state)).encode())
    for f in docs:
        h.update(f.name.encode() + f.read_bytes())
    _inputs[ident] = (time.monotonic(), stamp_, h.hexdigest())
    return _inputs[ident][2]


def last_words(log_path, limit=240):
    """The role's closing message from its log (Codex agent_message / Claude result) as one line."""
    text = ''
    with contextlib.suppress(OSError):
        for line in Path(log_path).open(errors='replace'):
            if line.startswith('{'):
                with contextlib.suppress(ValueError, AttributeError):
                    e = json.loads(line)
                    if e.get('type') == 'item.completed' and (e.get('item') or {}).get('type') == 'agent_message':
                        text = e['item'].get('text') or text
                    elif e.get('type') == 'result' and isinstance(e.get('result'), str):
                        text = e['result']
    return ' '.join(text.split())[:limit]


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
    # RUNNING: a role of this task is live (this runner tracks its own threads; a dead runner's
    # RUNNING marks are re-queued by recover() when the next runner takes the project lock).
    if auto.get('status') in {'BLOCKED', 'PAUSED', 'RUNNING'} or waiting_limit(auto):
        return None
    over = team.task_over_budget(root, ident, state)
    if over:
        if auto.get('status') != 'AWAITING_USER':
            update_runner_state(root, ident, status='AWAITING_USER', role='lead', run_id=None,
                                message=f'Görev bütçesi aşıldı (${over[0]:.2f} / ${over[1]:.2f}) — '
                                        f'devam: team budget --resume --task {ident}')
            notify('AI Team bütçe', f'{ident}: görev bütçesi aşıldı')
        return None
    unresolved = [g for g in state.get('gates', []) if g.get('status') != 'RESOLVED_BY_LEAD']
    if unresolved:
        if auto.get('status') != 'AWAITING_USER':
            update_runner_state(root, ident, status='AWAITING_USER', role='lead',
                                run_id=None, message='Human gate çözümü bekleniyor')
            notify('AI Team kullanıcı onayı bekliyor', f'{ident}: {unresolved[0].get("kind")}')
        return None
    deps = pending_deps(root, ident) if stage != 'NEW' else []
    if deps:  # Lead may scope a dependent task, but no work stage starts before its dependencies close
        msg = 'Bağımlılık bekleniyor: ' + ', '.join(deps) + ' kapanınca başlar'
        if auto.get('status') != 'WAITING_DEP' or auto.get('message') != msg:
            update_runner_state(root, ident, status='WAITING_DEP', role=None, run_id=None, message=msg)
        return None
    lim = team.limits(root).get('task_minutes')
    if lim and not state.get('time_override'):
        spent = sum(r.get('duration_s') or 0 for r in team.load_metrics(root)
                    if r.get('type') == 'run' and r.get('task') == ident) / 60
        if spent >= lim:
            if auto.get('status') != 'AWAITING_USER':
                update_runner_state(root, ident, status='AWAITING_USER', role=None, run_id=None,
                                    message=f'Görev süre sınırı aşıldı ({spent:.0f} / {lim:g} dk rol süresi) — '
                                            f'devam: team automate {ident} --resume')
                notify('AI Team süre sınırı', f'{ident}: {spent:.0f} dk')
            return None
    if stage == 'NEW' and state.get('intake'):
        return 'lead'  # Command Center task: Lead scopes it in a real run instead of waiting for a human
    if stage == 'NEW':
        if auto.get('status') != 'WAITING_FOR_LEAD':
            update_runner_state(root, ident, status='WAITING_FOR_LEAD', role='lead',
                                run_id=None, message='Lead kapsamı ve ilk aşamayı hazırlıyor')
        return None
    if auto.get('lead_turn') and STAGE_ROLE.get(stage) not in (None, 'lead'):
        return 'lead'  # a role stopped without advancing, or the user replied: Lead decides first
    role = STAGE_ROLE.get(stage)
    if not role:
        update_runner_state(root, ident, status='BLOCKED', role=None, run_id=None,
                            message='Bilinmeyen aşama: ' + stage)
        notify('AI Team durdu', f'{ident}: bilinmeyen aşama {stage}')
    stalled = auto.get('stalled') or {}
    if (role in ('tester', 'verifier') and (stalled.get('role'), stalled.get('stage')) == (role, stage)
            and stalled.get('inputs') == stage_inputs(root, ident, state, max_age=30)):
        # Same code, same task docs, no user reply: the same run would stop the same way (measured: one
        # verifier blocked 4x on an unanswered decision). Wait for the missing piece; `devam` forces a run.
        msg = (f'{role} aynı kod ve görev belgeleriyle zaten durdu; aynısını tekrar çalıştırmak yalnız token yakar. '
               f'Eksiği sağla (yanıt, karar, kanıt ya da kod değişince kendiliğinden başlar) ya da devam ile zorla. '
               f'Engel: {stalled.get("reason") or "-"}')
        if auto.get('status') != 'AWAITING_USER' or auto.get('message') != msg:
            update_runner_state(root, ident, status='AWAITING_USER', role=None, run_id=None, message=msg)
            notify('AI Team karar bekliyor', f'{ident}: {role} aynı engelde')
        return None
    return role


def run_task(root, ident, state, role, dry_run=False,
             turn_warning_seconds=DEFAULT_TURN_WARNING_MINUTES * 60,
             idle_seconds=DEFAULT_IDLE_TIMEOUT_MINUTES * 60,
             status_seconds=DEFAULT_STATUS_MINUTES * 60):
    stage = state['stage']
    run_id = str(uuid.uuid4())
    prompt = workflow_prompt(root, ident, state, role, run_id)
    command, provider, session_id = command_for(root, ident, state, role, prompt, run_id)
    # Interruptions of this role/stage so far (the marker survives a non-resumable cut too: backoff counts it).
    prev = (state.get('automation') or {}).get('resume') or {}
    attempt = ((prev.get('attempt') or 1) + 1 if (prev.get('role'), prev.get('stage'), prev.get('rework'))
               == (role, stage, state.get('rework_count', 0)) else 1)
    if runner_config(root).get('context_shadow', True):
        # SHADOW MODE: records a proposed context package next to the real one; never alters prompt/command.
        try:
            import intel
            intel.shadow_context(root, ident, role, prompt, run_id)
        except Exception as error:
            warn_once('_shadow', f'Shadow context atlandı: {error}')
    if dry_run:
        stamp(f'DRY RUN {ident}: {stage} → {role} ({provider})')
        return 'DRY_RUN'
    # Snapshot immediately before launch so a failed replace-style patch is recoverable.
    snapshot_dir = snapshot_task_markdown(root, ident, run_id)
    update_runner_state(root, ident, status='RUNNING', role=role, run_id=run_id, retry_at=None, resume=None,
                        message=f'{stage} aşaması' + (' kaldığı yerden sürüyor' if resume_marker(state, role) else ' çalışıyor'))
    if session_id:
        save_session(root, ident, role, provider, session_id, run_id)
    stamp(f'{ident}: {role} başladı · {stage}')
    ts_start, started = team.now(), time.monotonic()
    src = team.task_src(root, state)
    before = team.git_changes(src) if role == 'developer' else None
    expect = quota.expected(root, provider, role, engine_model(role, command)[1])
    with contextlib.suppress(Exception):  # the reservation only steers other starts; never block this one
        quota.reserve(run_id, provider, expect)
    try:
        code, discovered_session, log_path = run_process(
            root, ident, role, command, run_id, turn_warning_seconds, idle_seconds,
            status_seconds, **({'cwd': src} if state.get('worktree') else {}))
    finally:
        with contextlib.suppress(Exception):
            quota.release(run_id)
        if before is not None:
            record_changes(root, ident, before, src)
        # A deleted canonical report is restored even when the role process itself fails.
        restored = restore_missing_canonical_reports(root, ident, snapshot_dir)
        if restored:
            names = ', '.join(restored)
            stamp(f'{ident}: UYARI · kayıp rapor snapshot\'tan geri yüklendi: {names}')
            notify('AI Team raporu geri yükledi', f'{ident}: {names}')
    if discovered_session:
        save_session(root, ident, role, provider, discovered_session, run_id)
    _, after = load_state(root, ident)
    telemetry = TELEMETRY.pop(run_id, {})
    if provider == 'codex':  # Codex reports usage in its rollout file, not on stdout
        with contextlib.suppress(Exception):
            rollout = quota.codex_rollout(discovered_session)
            if rollout:
                telemetry['first'] = quota._tail_rate_limits(rollout, first=True)[0]
                telemetry['last'] = quota._tail_rate_limits(rollout)[0]
                full = [r for u, r in (telemetry['last'] or {}).values() if u >= 1]
                telemetry['reset'] = max(full) if full else None
            quota.observe_codex()
    spent = quota.window_delta(telemetry.get('first'), telemetry.get('last'))

    def measured(result, kind):
        usage = team.log_usage(log_path)
        engine, model = engine_model(role, command)
        row = {'type': 'run', 'ts_start': ts_start, 'ts_end': team.now(),
               'duration_s': round(time.monotonic() - started, 1), 'task': ident, 'role': role,
               'stage': stage, 'profile': state.get('profile'), 'engine': engine,
               'model': model or usage['model'], 'result': kind, 'run_id': run_id,
               'rework_no': state.get('rework_count', 0), 'tokens': usage['tokens'],
               'to': after.get('stage'),  # e.g. verifier -> IMPLEMENTATION = it sent the work back (quality metric)
               'cost_usd': usage['cost_usd']}
        if spent:  # this run's share of the subscription windows: calibrates quota.expected()
            row['quota'] = {'engine': engine, **spent}
        team.metric(root, row)
        return result

    if after.get('escalated') and not state.get('escalated'):
        esc = after['escalated']
        stamp(f'{ident}: YÜKSELTİLDİ · fix → small ({esc.get("reason")}: {", ".join(esc.get("files") or []) or "-"}); VERIFY_CODEX eklendi')
    if after.get('automation', {}).get('cancel_requested') == run_id:
        with team.locked(root):
            p, cur = load_state(root, ident)
            if role == 'developer' and (cur.get('developer') or {}).get('session') == run_id:
                cur['developer'] = None  # the cancelled run's own lock only
            cur.setdefault('automation', {}).update(status='BLOCKED', role=None, run_id=None, cancel_requested=None,
                                                    message=f'{role} kullanıcı tarafından iptal edildi', updated=team.now())
            cur['updated'] = team.now(); cur['revision'] = cur.get('revision', 0) + 1
            team.atomic(p / 'state.json', cur)
        stamp(f'{ident}: İPTAL EDİLDİ · {role}')
        return measured('BLOCKED', 'cancelled')
    failed = code != 0 or after['stage'] == stage
    kind, retry_at = interruption(log_path, attempt=attempt, reset_epoch=telemetry.get('reset')) if failed else (None, None)
    if kind:
        session = discovered_session or session_id
        resumable = bool(session) and attempt <= RESUME_ATTEMPTS and did_work(log_path)
        set_waiting_limit(root, ident, role, run_id, retry_at, kind=kind, resume={
            'role': role, 'provider': provider, 'session': session if resumable else None, 'stage': stage,
            'rework': state.get('rework_count', 0), 'kind': kind, 'attempt': attempt})
        why = 'kullanım limiti' if kind == 'limit' else 'ağ hatası'
        then = 'kaldığı yerden sürecek' if resumable else 'yeniden denenecek'
        notify('AI Team limit bekliyor' if kind == 'limit' else 'AI Team ağ hatası', f'{ident}: {role}, {retry_at.astimezone():%H:%M} {then}')
        stamp(f'{ident}: BEKLİYOR · {role} {why} · {retry_at.astimezone():%H:%M} sonra {then}')
        return measured('WAITING_LIMIT', kind)
    if code != 0:
        update_runner_state(root, ident, status='BLOCKED', role=role, run_id=run_id,
                            message=f'{role} çıkış kodu {code}; log: {log_path}')
        notify('AI Team rol hatası', f'{ident}: {role}, çıkış kodu {code}')
        stamp(f'{ident}: DURDU · {role} çıkış kodu {code} · {log_path}')
        return measured('BLOCKED', 'crash')
    if role in ('tester', 'verifier') and after['stage'] == stage:
        # Remember what this stop saw; triage() will not start an identical rerun.
        with contextlib.suppress(OSError, ValueError):
            after = update_runner_state(root, ident, stalled={
                'role': role, 'stage': stage, 'inputs': stage_inputs(root, ident, after), 'reason': last_words(log_path)})
    if after.get('automation', {}).get('status') == 'BLOCKED':
        # team.py itself blocked the task (rework limit, independence rule); keep its message.
        notify('AI Team kontrol gerekli', f'{ident}: {after["automation"].get("message")}')
        stamp(f'{ident}: DURDU · {after["automation"].get("message")}')
        return measured('BLOCKED', 'blocked')
    assist = role == 'lead' and STAGE_ROLE.get(stage) not in (None, 'lead')
    question = lead_question(root, ident) if (stage == 'NEW' or assist) and after['stage'] == stage else None
    if question:
        update_runner_state(root, ident, status='BLOCKED', role='lead', run_id=None,
                            message='Lead sorusu: ' + ' '.join(question.split())[:300])
        notify('AI Team Lead soruyor', f'{ident}: {question[:120]}')
        stamp(f'{ident}: Lead kullanıcı yanıtı bekliyor')
        return measured('BLOCKED', 'question')
    if assist:
        owner = STAGE_ROLE[stage]
        update_runner_state(root, ident, status='IDLE', role=None, run_id=None, lead_turn=None,
                            message=f'Lead karar verdi; {owner} devam ediyor' if after['stage'] == stage
                            else f'Lead: {stage} → {after["stage"]}')
        stamp(f'{ident}: Lead karar turu bitti · {stage} → {after["stage"]}')
        return measured('ADVANCED', 'decided')
    handoffs = (after.get('automation') or {}).get('lead_handoffs') or {}
    key = f'{stage}#{after.get("rework_count", 0)}'
    if after['stage'] == stage and role != 'lead' and handoffs.get(key, 0) < LEAD_HANDOFFS:
        # Terminal flow: the Lead (not the user) answers a role that stopped; the user is asked only if Lead can't decide.
        update_runner_state(root, ident, status='IDLE', role=None, run_id=None,
                            lead_turn={'role': role, 'reason': f'{role} aşamayı ilerletmedi', 'log': str(log_path)},
                            lead_handoffs={**handoffs, key: handoffs.get(key, 0) + 1},
                            message=f'{role} aşamayı ilerletmedi; Lead karar veriyor')
        stamp(f'{ident}: {role} aşamayı ilerletmedi → Lead karar turu')
        return measured('BLOCKED', 'handoff')
    if after['stage'] == stage:
        update_runner_state(root, ident, status='BLOCKED', role=role, run_id=run_id,
                            message=f'{role} aşamayı ilerletmedi; log: {log_path}')
        notify('AI Team kontrol gerekli', f'{ident}: {role} aşamayı ilerletmedi')
        stamp(f'{ident}: DURDU · {role} aşamayı ilerletmedi · {log_path}')
        return measured('BLOCKED', 'blocked')
    done = after['stage'] == 'DONE'
    label = f' · {team.pass_label(after)}' if done else ''
    update_runner_state(root, ident, status='COMPLETED' if done else 'IDLE',
                        role=None, run_id=None,
                        message=f'{stage} → {after["stage"]}{label}')
    stamp(f'{ident}: {stage} → {after["stage"]}{label}')
    if done:
        notify('AI Team tamamlandı', f'{ident}{label}')
    return measured('ADVANCED', 'advanced')


def dispatch_once(root, only=None, dry_run=False,
                  turn_warning_seconds=DEFAULT_TURN_WARNING_MINUTES * 60,
                  idle_seconds=DEFAULT_IDLE_TIMEOUT_MINUTES * 60,
                  status_seconds=DEFAULT_STATUS_MINUTES * 60):
    """Run the first runnable task to completion (sequential; used by `once`)."""
    if team.paused():
        stamp('Yeni görev başlatma duraklatıldı (Command Center / menü çubuğu: Resume)')
        return 'PAUSED'
    if team.budget_state(root)['paused']:
        stamp('Bütçe doldu — duraklatıldı (team budget --resume ya da runner.json)')
        return 'PAUSED'
    tasks = sorted(enabled_tasks(root, only), key=lambda t: -progress(t[1]))
    for ident, state in tasks:
        role = triage(root, ident, state)
        if not role:
            continue
        held = None if dry_run else hold(root, ident, state, role, runner_config(root), tasks)
        if held:
            park(root, ident, state, *held)
            continue
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


_preflight = {}  # (root, command) -> (ok, detail, checked monotonic)
PREFLIGHT_TTL = 60.0


def preflight(root, role, cfg, state=None):
    """First failing environment check for `role` from runner.json "preflight" ({role or "*": [command, ...]}),
    e.g. {"tester": ["docker info"]}, plus the verifier's sandbox probe. Run by the runner (no tokens), cached
    a minute; None when all pass."""
    checks = cfg.get('preflight') or {}
    items = [(f'`{c}`', c, root) for c in list(checks.get(role) or []) + list(checks.get('*') or [])]
    probe = sandbox_probe(root, state) if role == 'verifier' else None
    for label, cmd, cwd in items + ([probe] if probe else []):
        key = (str(cwd), str(cmd))
        hit = _preflight.get(key)
        if not hit or time.monotonic() - hit[2] > PREFLIGHT_TTL:
            try:
                argv = shlex.split(cmd) if isinstance(cmd, str) else cmd
                r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
                hit = (r.returncode == 0, ' '.join((r.stderr or r.stdout).split())[-160:], time.monotonic())
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                hit = (False, str(error)[-160:], time.monotonic())
            _preflight[key] = hit
        if not hit[0]:
            return f'{label} başarısız' + (f': {hit[1]}' if hit[1] else '')
    return None


# Stages whose evidence fingerprints the shared checkout: another task's Developer must not edit it meanwhile.
FROZEN_STAGES = {'TEST_CLAUDE', 'VERIFY_CODEX', 'KNOWLEDGE_UPDATE'}


def hold(root, ident, state, role, cfg, others=()):
    """Why `role` must not start yet as (status, message, retry epoch or None), else None. No check spends tokens."""
    bad = preflight(root, role, cfg, state)
    if bad:
        return 'WAITING_ENV', f'Ortam hazır değil: {bad}; düzelince kendiliğinden başlar', None
    if role == 'developer' and not state.get('worktree'):
        scope = team.task_scope(team.task(root, ident))
        for other, ostate in others:
            if (other != ident and ostate.get('stage') in FROZEN_STAGES and not ostate.get('worktree')
                    and team.scopes_conflict(scope, team.task_scope(team.task(root, other)))):
                return 'IDLE', f'{other} test/doğrulamada ve kapsam çakışıyor; o bitince başlar (kanıtı geçersiz olmasın)', None
    qcfg = cfg.get('quota') or {}
    if qcfg.get('enabled', True):
        engine = 'claude' if role in CLAUDE_ROLES else 'codex'
        model = role_model(root, state, role) if engine == 'claude' else cfg.get('codex_model')
        ok, until, why = quota.admit(engine, quota.expected(root, engine, role, model), (qcfg.get('reserve') or {}).get(engine))
        if not ok:
            return 'WAITING_LIMIT', f'Kota: {why}; {datetime.fromtimestamp(until):%H:%M} sonra başlar', until
    return None


def park(root, ident, state, status, message, until=None):
    """Show why a runnable task waits; written only on change (the loop polls every few seconds)."""
    auto = state.get('automation') or {}
    if auto.get('status') == status and auto.get('message') == message:
        return
    fields = {'status': status, 'role': None, 'run_id': None, 'message': message}
    if until:
        fields['retry_at'] = datetime.fromtimestamp(until, timezone.utc).isoformat()
    update_runner_state(root, ident, **fields)
    stamp(f'{ident}: BEKLİYOR · {message}')
    if status == 'WAITING_ENV' and auto.get('status') != 'WAITING_ENV':
        notify('AI Team ortam bekliyor', f'{ident}: {message[:120]}')


def progress(state):
    """Position of the task in its own flow: the runner starts the task nearest to DONE first."""
    path = state.get('stage_path') or []
    return path.index(state['stage']) / max(1, len(path)) if state.get('stage') in path else 0


def run_task_safely(root, ident, state, role, *timing):
    try:
        run_task(root, ident, state, role, False, *timing)
    except Exception as error:  # a failed launch must not kill the other tasks
        update_runner_state(root, ident, status='BLOCKED', role=role, run_id=None,
                            message=f'{role} başlatılamadı: {error}')
        stamp(f'{ident}: DURDU · {role} başlatılamadı: {error}')


CLAUDE_POLL = 600  # seconds: an older Claude usage snapshot is refreshed before it steers admission


def watch_loop(root, only, poll, timing, once_round=False):
    """Start every admissible runnable task in its own thread; poll until interrupted."""
    running = {}  # ident -> (thread, role, scope)
    codex_seen = claude_seen = 0.0
    while True:
        if time.monotonic() - codex_seen > 60:  # Codex usage lives in its rollout files (any session counts)
            codex_seen = time.monotonic()
            with contextlib.suppress(Exception):
                quota.observe_codex()
            # Role runs report Claude usage as they go; while they don't, the user's own sessions still move
            # the window, so a stale snapshot is refreshed with /usage (no tokens), off the loop thread.
            with contextlib.suppress(Exception):
                age = quota._now() - ((quota.load().get('claude') or {}).get('at') or 0)
                if not once_round and age > CLAUDE_POLL and time.monotonic() - claude_seen > CLAUDE_POLL:
                    claude_seen = time.monotonic()
                    threading.Thread(target=quota.observe_claude, args=(executable('claude'),), daemon=True).start()
        for ident in [i for i, (t, _, _) in running.items() if not t.is_alive()]:
            del running[ident]
        try:
            cfg = runner_config(root)
            tasks = enabled_tasks(root, only)
            budget = team.budget_state(root)
            for line in budget['lines']:  # once per threshold level, not per poll
                warn_once('_budget' + line.split(':')[0] + line.split(' ')[2], line, line.split(' $')[0])
            if budget['paused']:
                tasks = []  # running roles finish; no new role starts
            if team.paused():
                warn_once('_pause', 'Duraklatıldı: yeni rol başlamaz, çalışan roller sürer.')
                tasks = []
            else:
                _warned.pop('_pause', None)
        except Exception as error:  # e.g. a broken runner.json; keep the loop alive and retry
            warn_once('_round', f'Runner turu atlandı: {error}')
            tasks = []
        tasks = sorted(tasks, key=lambda t: -progress(t[1]))  # finish started work before starting new work
        for ident, state in tasks:
            if ident in running:
                continue
            try:
                role = triage(root, ident, state)
                if not role:
                    continue
                scope = team.task_scope(team.task(root, ident))
                if state.get('worktree'):
                    scope = [f'\0worktree/{ident}/**']  # its own tree: overlaps no other task
                # A manual/foreign source lock (or an unreadable state: OSError, fail-closed) defers the
                # Developer to a later round instead of launching a run whose claim must fail.
                if role == 'developer' and team.lock_conflict(root, scope, ident, bool(state.get('worktree'))):
                    continue
                held = hold(root, ident, state, role, cfg, tasks)
                if held:
                    park(root, ident, state, *held)
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


LOCK_RETRY_SECONDS = 60.0


def recover(root):
    """Called once the runner holds the project lock: no role of a previous runner can still be running.
    RUNNING tasks are re-queued at the same stage (evidence is fingerprinted, so a half-written
    result is re-checked, never trusted); the dead run's own developer lock is released."""
    found = []
    for state_file in sorted((root / 'tasks').glob('*/state.json')):
        try:
            if (team.read(state_file).get('automation') or {}).get('status') != 'RUNNING':
                continue
        except (OSError, ValueError):
            continue
        ident = state_file.parent.name
        with team.locked(root):
            p, state = load_state(root, ident)
            auto = state.setdefault('automation', {})
            if auto.get('status') != 'RUNNING':
                continue
            run_id, role = auto.get('run_id'), auto.get('role')
            if state.get('developer') and state['developer'].get('session') == run_id:
                state['developer'] = None
                base = team.claim_baseline(root, ident)
                if base.is_file():  # keep what the dead run already changed in the commit set
                    team.add_changed(p, state, team.read(base), team.git_changes(team.task_src(root, state)))
                    base.unlink()
            auto.setdefault('warnings', []).append({'kind': 'INTERRUPTED', 'role': role, 'run_id': run_id,
                                                    'created': team.now()})
            auto.update(status='WAITING_FOR_LEAD' if state['stage'] == 'NEW' else 'IDLE', role=None, run_id=None,
                        cancel_requested=None, updated=team.now(),
                        message=f'Runner yeniden başladı; yarıda kalan {role} turu {state["stage"]} aşamasından yeniden çalışacak')
            state['updated'] = team.now(); state['revision'] = state.get('revision', 0) + 1
            team.atomic(p / 'state.json', state)
        found.append(ident)
        stamp(f'{ident}: KURTARMA · yarıda kalan {role} turu yeniden sıraya alındı')
    return found


@contextlib.contextmanager
def runner_lock(root, wait=False):
    """Single watcher per project. With wait (watch), a held lock is retried every LOCK_RETRY_SECONDS
    with one log line, so a service manager's KeepAlive does not respawn-loop and flood the log."""
    path = root / '.ai' / '.runner.lock'
    with path.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise RuntimeError('Bu proje için Team Runner zaten çalışıyor.')
                warn_once('_lock', f'Başka bir Team Runner çalışıyor; {LOCK_RETRY_SECONDS:g} sn arayla bekleniyor.')
                time.sleep(LOCK_RETRY_SECONDS)
        _warned.pop('_lock', None)
        pid = root / '.ai' / '.runner.pid'
        pid.write_text(str(os.getpid()))  # Command Center "online" probe; the lock stays the authority
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                pid.unlink()


def agent_env():
    # PATH from the installing shell; the verifier engine switch is read at runner start, not from runner.json.
    env = {'PATH': headless_path(), 'TEAM_VERIFIER_ENGINE': os.environ.get('TEAM_VERIFIER_ENGINE')}
    return {k: v for k, v in env.items() if v}


def agent_unit(root, platform=None):
    """(path, content, load command, unload command) of a per-project user service running `watch`."""
    platform = platform or sys.platform
    pid = re.sub(r'[^A-Za-z0-9_.-]', '-', str(team.read(root/'.ai/project.json').get('id') or root.name))
    log = root/'.ai'/'runner-logs'/'_team_runner.log'
    args = [sys.executable, str(Path(__file__).resolve()), '--project', str(root), 'watch']
    if platform == 'darwin':
        label = f'com.ai-orchestra.runner.{pid}'
        path = Path.home()/'Library'/'LaunchAgents'/f'{label}.plist'
        body = plistlib.dumps({'Label': label, 'ProgramArguments': args, 'RunAtLoad': True,
                               'KeepAlive': True, 'WorkingDirectory': str(root),
                               'StandardOutPath': str(log), 'StandardErrorPath': str(log),
                               'EnvironmentVariables': agent_env()}).decode()
        quoted = shlex.quote(str(path))
        return path, body, f'launchctl bootstrap gui/$(id -u) {quoted}', f'launchctl bootout gui/$(id -u) {quoted}'
    name = f'ai-orchestra-runner-{pid}.service'
    path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'systemd'/'user'/name
    body = (f'[Unit]\nDescription=ai-orchestra runner ({pid})\n\n[Service]\n'
            f'ExecStart={shlex.join(args)}\nWorkingDirectory={root}\n'
            + ''.join(f'Environment="{k}={v}"\n' for k, v in agent_env().items()) +
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
    with runner_lock(root, wait=args.command == 'watch'):
        recover(root)
        if args.command == 'once':
            dispatch_once(root, args.task, args.dry_run,
                          args.turn_warning_minutes * 60,
                          args.idle_timeout_minutes * 60); return
        stamp(f'Team Runner hazır · {root}')
        stamp('Yalnız --auto görevler çalışır; NEW aşamasında Lead beklenir. Ctrl-C ile durdurabilirsiniz.')
        watch_loop(root, args.task, args.poll, (args.turn_warning_minutes * 60,
                   args.idle_timeout_minutes * 60, args.status_minutes * 60))


def _terminate(signum, frame):
    raise KeyboardInterrupt  # launchd/systemd stop: same clean path as Ctrl-C


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _terminate)
    try:
        main()
    except KeyboardInterrupt:
        for proc in list(LIVE):  # no orphan role may outlive its runner (recover() would start it twice)
            stop_process(proc)
        stamp('Team Runner durduruldu.')
    except (RuntimeError, ValueError, OSError) as error:
        print('STOP:', error, file=sys.stderr)
        sys.exit(2)
