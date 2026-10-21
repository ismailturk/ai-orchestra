#!/usr/bin/env python3
"""Bir rolun en guncel Team Runner logunu izler ve panele insan okunur basar.

Kullanim: role_view.py <proje-yolu> <rol>

Rolleri Team Runner calistirir; bu betik yalnizca IZLER. Hicbir surec baslatmaz,
hicbir dosya yazmaz. Yeni bir kosu basladiginda otomatik olarak yeni loga gecer.
"""
import glob
import json
import os
import sys
import time

STAMP = '[%H:%M:%S]'


def stamp(message):
    print(time.strftime(STAMP), message, flush=True)


def render(event):
    """team_runner.render_event ile ayni bicim; burada rol adi tekrar edilmez."""
    kind = event.get('type')
    item = event.get('item') or {}
    itype = item.get('type')
    if kind == 'thread.started':
        stamp(f'· oturum {event.get("thread_id", "?")}')
    elif kind == 'turn.started':
        stamp('· tur başladı')
    elif kind == 'item.started' and itype == 'command_execution':
        lines = (item.get('command') or '').strip().splitlines()
        if lines:
            text = lines[0].strip()
            if len(lines) > 1:
                text += f' … (+{len(lines) - 1} satır)'
            stamp(f'$ {text[:400]}')
    elif kind == 'item.completed' and itype == 'command_execution':
        code = item.get('exit_code')
        if code not in (None, 0):
            stamp(f'! çıkış kodu {code}')
    elif kind == 'item.completed' and itype == 'agent_message':
        for line in (item.get('text') or '').strip().splitlines():
            if line.strip():
                stamp(f'› {line.strip()[:400]}')
    elif kind == 'item.completed' and itype in {'file_change', 'patch_apply'}:
        detail = item.get('status') or item.get('path') or ''
        stamp(f'· dosya değişikliği {detail}'.rstrip())
    elif kind in {'error', 'turn.failed'}:
        stamp(f'! {json.dumps(event, ensure_ascii=False)[:400]}')
    else:
        render_claude(event)


def render_claude(event):
    """Claude Code --output-format stream-json olaylari."""
    kind = event.get('type')
    if kind == 'system' and event.get('subtype') == 'init':
        stamp(f'· oturum {event.get("session_id", "?")}')
    elif kind == 'assistant':
        for block in (event.get('message') or {}).get('content') or []:
            btype = block.get('type')
            if btype == 'text':
                for line in (block.get('text') or '').strip().splitlines():
                    if line.strip():
                        stamp(f'› {line.strip()[:400]}')
            elif btype == 'tool_use':
                stamp(f'$ {tool_summary(block)[:400]}')
    elif kind == 'user':
        for block in (event.get('message') or {}).get('content') or []:
            if block.get('type') == 'tool_result' and block.get('is_error'):
                text = str(block.get('content') or '').strip().replace('\n', ' ')
                stamp(f'! {text[:400]}')
    elif kind == 'result':
        turns = event.get('num_turns')
        detail = f' · {turns} tur' if turns else ''
        stamp(f'· sonuç: {event.get("subtype", "?")}{detail}')


def tool_summary(block):
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


def newest_log(root, role):
    pattern = os.path.join(root, '.ai', 'runner-logs', '*', f'{role}-*.log')
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else None


def follow(path):
    """Dosyayi sonuna kadar okur, sonra yeni satirlari bekler.

    Daha yeni bir log dosyasi belirirse cagirana doner.
    """
    with open(path, encoding='utf-8', errors='replace') as handle:
        while True:
            line = handle.readline()
            if line:
                line = line.strip()
                if not line:
                    continue
                try:
                    render(json.loads(line))
                except json.JSONDecodeError:
                    stamp(line[:400])
                continue
            yield
            time.sleep(0.4)


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    root, role = sys.argv[1], sys.argv[2]
    if not os.path.isdir(root):
        print(f'STOP: proje bulunamadı: {root}', file=sys.stderr)
        return 2

    stamp(f'{role} paneli · Team Runner çıktısı izleniyor')
    stamp('Bu panel yalnız izler; rolü Team Runner başlatır.')
    current = None
    while True:
        latest = newest_log(root, role)
        if latest is None:
            time.sleep(2)
            continue
        if latest != current:
            current = latest
            stamp(f'── {os.path.basename(latest)}')
            reader = follow(latest)
        try:
            next(reader)
        except StopIteration:
            reader = None
            current = None
            continue
        if newest_log(root, role) != current:
            continue


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
