#!/usr/bin/env python3
"""AI Orchestra terminal UI.

  team-tui [proje]        Lead (interaktif claude) solda, canlı pano sağda (cmux split); runner'ı başlatır.
                          Proje verilmezse bulunduğun klasör; kurulu değilse kurulum için Claude açılır.
  team-tui board <proje>  yalnız pano

Pano salt-okunur bakar; yazdığı tek şeyler kullanıcının açık komutlarıdır (yanıt, iptal, devam, runner, duraklat).
"""
import contextlib
import curses
import locale
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import orchestra  # noqa: E402
import quota  # noqa: E402
import team  # noqa: E402

CMUX = '/Applications/cmux.app/Contents/Resources/bin/cmux'
SHORT = {'NEW': 'NEW', 'ANALYSIS': 'ANLZ', 'DESIGN': 'DSGN', 'IMPLEMENTATION': 'IMPL', 'TEST_CLAUDE': 'TEST',
         'VERIFY_CODEX': 'VRFY', 'KNOWLEDGE_UPDATE': 'KNOW', 'DONE': 'DONE', 'CLOSED': 'CLSD', 'ABANDONED': 'ABND'}
ROLE_TR = {'lead': 'Lead', 'analyst': 'Analist', 'designer': 'Tasarımcı', 'developer': 'Developer',
           'tester': 'Tester', 'verifier': 'Verifier'}
LEAD_PROMPT = """Bu oturum AI Orchestra LEAD oturumudur. Proje: {name} ({root}).
roles/lead.md Lead kurallarını uygula. Kullanıcıyla anlık konuş, isteği netleştir; kodu yalnız oku, üretim kodu yazma.
Görev hazır olunca kendin aç (runner'ın ayrı Lead girişi atlanır: daha hızlı, daha az token):
  team new <ID> "<başlık>" --profile <fix|small|ui|full|risky> --auto --worktree --files '<glob>'
  → tasks/<ID>/TASK.md: amaç, kabul kriterleri, en dar `## Dosyalar` → team advance <ID> <ilk aşama>
ID biçimi: T<AAGG-SSDD>. En hafif güvenli profili seç; dar hata = fix.
Büyük işi tek turda bitecek dilimlere böl (her dilim ayrı görev); sıralı dilimde TASK.md "## Bağımlılıklar" altına
öncekinin ID'sini yaz, runner o kapanınca başlatır. Kota/limit durumunu pano başlığı gösterir; runner sığmayan rolü
sıfırlanmaya kadar bekletir, limitte kesilen rolü kaldığı yerden sürdürür.
Sağ paneldeki pano görevleri canlı gösterir; durum için `team status <ID>` yeter, gerekmedikçe log/rapor okuma.
Rol durursa runner önce kendi Lead karar turunu çalıştırır; görev yine BLOCKED kalırsa kullanıcıyla konuş, sonra
`team reply <ID> --text "..."` ya da `team automate <ID> --resume`. DONE sonrası `team commit <ID>` taslağını göster.
Human gate (DB, deploy, push/merge, gerçek veri/hesap) yalnız kullanıcının bu konuşmadaki açık onayıyla."""


SETUP_PROMPT = """Bu klasörde ({root}) AI Orchestra henüz kurulu değil. Kullanıcı "gerekli kurulumları yap" (ya da benzerini)
derse orchestra-setup becerisini uygula. Kurulum bitince kullanıcıya bu oturumdan çıkıp (/exit) aynı klasörde
`team-tui` komutunu yeniden çalıştırmasını söyle: Lead, canlı pano ve runner o zaman açılır."""


def resolve(name):
    try:
        return name, orchestra.root_of(name)
    except ValueError:
        p = Path(name).expanduser()
        if (p / '.ai/PROJECT.md').is_file():
            return team.register(p), team.project(p)  # a path works too; registering makes `team-tui <ad>` work next time
        raise


def launch(name):
    try:
        name, root = resolve(name)
    except ValueError as error:
        p = Path(name).expanduser()
        if not p.is_dir():
            sys.exit(f'{error}. Kayıtlı projeler: ' + (', '.join(orchestra.registry()) or '-'))
        # Not set up yet: no runner or board to show; open Claude here for the setup skill.
        print('Bu klasörde AI Orchestra kurulu değil. Claude açılıyor: "bu projede gerekli kurulumları yap" de;'
              ' bitince /exit ve tekrar team-tui.')
        os.chdir(p)
        os.execvp('claude', ['claude', '--append-system-prompt', SETUP_PROMPT.format(root=p.resolve())])
        return
    print('Runner:', orchestra.start_runner(root))
    board = f'{shlex.quote(sys.executable)} {shlex.quote(str(BIN / "tui.py"))} board {shlex.quote(str(root))}'
    if os.environ.get('CMUX_WORKSPACE_ID') and Path(CMUX).exists():
        r = subprocess.run([CMUX, 'new-split', 'right', '--command', board, '--focus', 'false'],
                           capture_output=True, text=True, env={**os.environ, 'CMUX_QUIET': '1'})
        if r.returncode:
            print('Pano açılamadı:', (r.stderr or r.stdout).strip())
    else:
        print('cmux dışında: panoyu ikinci bir terminalde aç →', board)
    os.chdir(root)
    os.execvp('claude', ['claude', '--append-system-prompt', LEAD_PROMPT.format(name=name, root=root)])


# ---------- data ----------

def latest_log(root, ident):
    logs = sorted((root / '.ai/runner-logs' / ident).glob('*.log'), key=lambda f: f.stat().st_mtime)
    return logs[-1] if logs else None


def since(ts):
    if not ts:
        return ''
    try:
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return ''
    return f'{s:.0f}sn' if s < 60 else f'{s / 60:.0f}dk' if s < 3600 else f'{s / 3600:.1f}sa' if s < 86400 else f'{s / 86400:.0f}g'


def runs(metrics, ident):
    """Per-run rows for one task; fresh = input+output+cache_creation (full price), cache = cache reads (~1/10 price)."""
    out = []
    for r in metrics:
        if r.get('type') == 'run' and r.get('task') == ident:
            tok = r.get('tokens') or {}
            out.append({'role': r.get('role'), 'model': r.get('model') or r.get('engine') or '-', 'result': r.get('result') or '-',
                        'sec': r.get('duration_s') or 0, 'cache': tok.get('cache_read') or 0, 'tokens': tok,
                        'quota': r.get('quota') if isinstance(r.get('quota'), dict) else {},
                        'fresh': sum(tok.get(k) or 0 for k in team.TOKEN_KEYS if k != 'cache_read'), 'usd': r.get('cost_usd') or 0})
    return out


def token_sum(rows):
    """{input, output, cache_read, cache_creation} summed over metric or runs() rows."""
    return {k: sum((r.get('tokens') or {}).get(k) or 0 for r in rows) for k in team.TOKEN_KEYS}


def until(epoch):
    s = max(0, epoch - time.time())
    return f'{s / 60:.0f}dk' if s < 3600 else f'{s // 3600:.0f}sa {s % 3600 // 60:.0f}dk' if s < 86400 else f'{s // 86400:.0f}g {s % 86400 // 3600:.0f}sa'


def spark(values, width=12):
    bars = '▁▂▃▄▅▆▇█'
    values = values[-width:]
    top = max(values or [0]) or 1
    return ''.join(bars[min(7, int(v / top * 7.999))] for v in values)


def human(n):
    return f'{n / 1e6:.1f}M' if n >= 1e6 else f'{n / 1e3:.0f}K' if n >= 1e3 else f'{n:.0f}'


def needs_user(t):
    return t.get('stage') not in team.CLOSED_STAGES and (t.get('status') in ('BLOCKED', 'AWAITING_USER', 'STATE_ERROR') or bool(t.get('gates')))


def visible(rows):
    """Open tasks (waiting on the user first, then running), then the five latest closed ones."""
    open_ = [t for t in rows if t['stage'] not in team.CLOSED_STAGES]
    open_.sort(key=lambda t: (not needs_user(t), t['status'] != 'RUNNING'))
    return open_ + [t for t in rows if t.get('stage') in team.CLOSED_STAGES][:5]


# ---------- board ----------

SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
BLOCKS = ' ▏▎▍▌▋▊▉'
SPARKLE = '✦✧⋆✧'
LOGO = ['   ▄▀█ █   █▀█ █▀█ █▀▀ █ █ █▀▀ █▀ ▀█▀ █▀█ ▄▀█',
        '   █▀█ █   █▄█ █▀▄ █▄▄ █▀█ ██▄ ▄█  █  █▀▄ █▀█']
# 256-colour palette (fallback: 8 colours): name -> (256, 8)
PALETTE = {'text': (252, 7), 'dim': (244, 7), 'faint': (238, 0), 'acc': (141, 5), 'run': (81, 6), 'ok': (114, 2),
           'warn': (221, 3), 'bad': (203, 1), 'cmd': (110, 4), 'hot': (213, 5), 'glow': (231, 7)}
GRADIENT = [99, 105, 111, 117, 123, 159]


class Board:
    def __init__(self, scr, root):
        self.scr, self.root, self.sel, self.info, self.note = scr, root, 0, False, ''
        self.rows, self.loaded, self.online, self.paused, self.quota = [], 0, None, False, {}
        self.metrics, self.metrics_key, self.log_cache = [], None, (None, [])
        self.born, self.tween, self.seen, self.flash, self.event = time.monotonic(), {}, None, {}, None
        curses.curs_set(0); curses.use_default_colors(); scr.timeout(120)
        rich = curses.COLORS >= 256
        self.C, n = {}, 1
        for name, (c256, c8) in PALETTE.items():
            curses.init_pair(n, c256 if rich else c8, -1); self.C[name] = curses.color_pair(n); n += 1
        self.C['dim'] |= 0 if rich else curses.A_DIM
        self.C['glow'] |= curses.A_BOLD
        self.grad = []
        for c in (GRADIENT if rich else [5, 5, 4, 4, 6, 6]):
            curses.init_pair(n, c, -1); self.grad.append(curses.color_pair(n) | curses.A_BOLD); n += 1
        self.chip = {}
        for name, bg in (('acc', 141 if rich else 5), ('ok', 114 if rich else 2), ('warn', 221 if rich else 3),
                         ('bad', 203 if rich else 1), ('run', 81 if rich else 6), ('key', 237 if rich else 0)):
            curses.init_pair(n, 252 if name == 'key' and rich else 16 if rich else 0, bg)
            self.chip[name] = curses.color_pair(n) | (0 if name == 'key' else curses.A_BOLD); n += 1

    # ----- primitives -----
    def put(self, y, x, text, attr=0, end=None):
        h, w = self.scr.getmaxyx()
        n = min(w - 1, end or w) - x
        if 0 <= y < h and 0 <= x < w - 1 and n > 0:
            try:
                self.scr.addnstr(y, x, text, n, attr)
            except curses.error:
                pass
        return x + len(text)

    def box(self, y, x, h, w, title='', attr=None, right=''):
        a = self.C['faint'] if attr is None else attr
        self.put(y, x, '╭' + '─' * (w - 2) + '╮', a)
        for i in range(1, h - 1):
            self.put(y + i, x, '│', a); self.put(y + i, x + w - 1, '│', a)
        self.put(y + h - 1, x, '╰' + '─' * (w - 2) + '╯', a)
        if title:
            self.put(y, x + 2, f' {title} ', self.C['acc'] | curses.A_BOLD)
        if right:
            self.put(y, x + w - len(right) - 3, f' {right} ', self.C['dim'])

    def frame(self):
        return int(time.monotonic() * 8)

    def ease(self, key, target):
        """Shown value glides a quarter of the way to target each frame, so numbers and bars animate instead of jumping."""
        v = self.tween.get(key, 0)
        v = target if abs(target - v) <= abs(target) * 0.002 + 0.5e-3 else v + (target - v) * 0.25
        self.tween[key] = v
        return v

    def meter(self, y, x, frac, width, tone, sheen=False):
        """Bar with 1/8-cell precision; sheen = a light glint sweeping the filled part (that engine is in use now)."""
        cells = max(0.0, min(1.0, frac)) * width
        full = int(cells)
        text = '█' * full + (BLOCKS[int((cells - full) * 8)].strip() if full < width else '')
        end = self.put(y, x, text, tone)
        end = self.put(y, end, '░' * (width - len(text)), self.C['faint'])
        k = self.frame() % (full + 16)
        if sheen and k < full:
            self.put(y, x + k, '█', self.C['glow'])
        return end

    def sweep(self, y, x, w, t0, attr):
        """A highlight band crossing a row once, ~1 s after t0, without redrawing its text."""
        p = (time.monotonic() - t0) / 1.0
        if 0 <= p < 1:
            a = x + int(p * (w + 8)) - 8
            lo, hi = max(x, a), min(x + w, a + 8)
            if hi > lo:
                with contextlib.suppress(curses.error):
                    self.scr.chgat(y, lo, hi - lo, attr)

    # ----- data -----
    def measure(self):
        """While someone watches, keep the limits live: Codex from its rollout files every 15 s, Claude via
        /usage (0 tokens, ~5 s) once a minute; the runner alone refreshes only every 10 min."""
        now = time.monotonic()
        if now - getattr(self, 'codex_at', -1e9) > 15:
            self.codex_at = now
            with contextlib.suppress(Exception):
                quota.observe_codex()
        age = time.time() - ((self.quota.get('claude') or {}).get('at') or 0)
        busy = getattr(self, 'probe', None)
        if age > 60 and not (busy and busy.is_alive()):
            with contextlib.suppress(Exception):
                self.probe = threading.Thread(target=quota.observe_claude,
                                              args=(orchestra.team_runner.executable('claude'),), daemon=True)
                self.probe.start()

    def refresh_data(self):
        if time.monotonic() - self.loaded < 1.5:
            return
        self.rows, self.loaded = visible(orchestra.task_rows(self.root)), time.monotonic()
        self.online, self.paused = orchestra.runner_online(self.root), team.paused()
        self.quota = quota.load()
        self.measure()
        self.notice()
        f = self.root / '.ai/metrics.jsonl'
        key = f.stat().st_mtime if f.is_file() else 0
        if key != self.metrics_key:
            self.metrics, self.metrics_key = team.load_metrics(self.root), key

    def notice(self):
        """Remember which tasks just moved: their rows flash and the footer announces it for a few seconds."""
        now, cur = time.monotonic(), {t['id']: t for t in self.rows}
        for i, t in cur.items():
            old = (self.seen or {}).get(i)
            if old is None or (old.get('stage'), old.get('status')) == (t.get('stage'), t.get('status')):
                continue
            self.flash[i] = now
            if t.get('stage') == 'DONE':
                self.event = (now, f'✦ {i} tamamlandı', 'ok')
            elif needs_user(t) and not needs_user(old):
                self.event = (now, f'⚑ {i} seni bekliyor', 'warn')
            elif old.get('stage') != t.get('stage'):
                self.event = (now, f"→ {i}  {SHORT.get(old.get('stage'), old.get('stage'))} → {SHORT.get(t.get('stage'), t.get('stage'))}", 'run')
        self.seen = cur

    def today(self):
        day = time.strftime('%Y-%m-%d', time.gmtime())
        rs = [r for r in self.metrics if r.get('type') == 'run' and (r.get('ts_end') or '').startswith(day)]
        return len(rs), token_sum(rs), sum(r.get('cost_usd') or 0 for r in rs)

    def tokens(self, y, x, tok, end, key=None):
        """One line: full-price tokens split by kind, then cache reads (billed ~1/10); as detailed as fits before end."""
        if key:
            tok = {k: self.ease(f'{key}:{k}', v) for k, v in tok.items()}
        full, i, o, cw, cr = (human(v) for v in (tok['input'] + tok['output'] + tok['cache_creation'],
                                                 tok['input'], tok['output'], tok['cache_creation'], tok['cache_read']))
        for parts in ((f'tam fiyat {full}', f' (girdi {i} · çıktı {o} · önbelleğe yazma {cw})', f' + önbellek okuma {cr} (~1/10 fiyat)'),
                      (f'tam {full}', f' (girdi {i} · çıktı {o} · yazma {cw})', f' + önbellek {cr}'),
                      (f'tam {full}', '', f' + önbellek {cr}')):
            if x + sum(map(len, parts)) <= end:
                break
        for text, a in zip(parts, (self.C['hot'] | curses.A_BOLD, self.C['text'], self.C['dim'])):
            x = self.put(y, x, text, a, end)
        return x

    def log_lines(self, ident):
        log = latest_log(self.root, ident)
        key = (log, log.stat().st_mtime) if log else None
        if key != self.log_cache[0]:
            lines = [orchestra.intel.redact(l) for l in orchestra.render_log(log, 200)] if log else []
            # raw JSON errors -> just their message
            lines = [('! ' + m.group(1)) if l.startswith('! {') and (m := re.search(r'"message": "(.*?)"', l)) else l for l in lines]
            self.log_cache = (key, [l for i, l in enumerate(lines) if not i or l != lines[i - 1]])
        return log, self.log_cache[1]

    # ----- look -----
    def glyph(self, t):
        if t.get('stage') == 'DONE':
            return '✓', self.C['ok']
        if t.get('stage') in team.CLOSED_STAGES:
            return '–', self.C['dim']
        if t.get('status') == 'RUNNING':
            return SPIN[self.frame() % len(SPIN)], self.C['run'] | curses.A_BOLD
        if t.get('status') in ('WAITING_LIMIT', 'WAITING_DEP', 'WAITING_ENV'):
            return '◷', self.C['warn']
        if needs_user(t):
            pulse = curses.A_BOLD if self.frame() // 4 % 2 else 0
            return ('?' if (t.get('message') or '').startswith('Lead sorusu') else '⚑' if t.get('gates') or t['status'] == 'AWAITING_USER' else '■'), \
                (self.C['warn'] if t['status'] == 'AWAITING_USER' or t.get('gates') else self.C['bad']) | pulse
        return '·', self.C['dim']

    def state_text(self, t):
        if t.get('stage') == 'DONE':
            return (t.get('label') or 'DONE').replace(' (bağımsız)', ' bağımsız')
        if t.get('stage') in team.CLOSED_STAGES:
            return 'kapandı'
        if t.get('status') == 'RUNNING':
            return ROLE_TR.get(t['role'], t['role'] or '?') + ' çalışıyor'
        if (t.get('message') or '').startswith('Lead sorusu'):
            return 'Lead soruyor'
        if t.get('gates'):
            return 'onay bekliyor'
        if t.get('status') == 'WAITING_LIMIT' and (t.get('message') or '').startswith('Kota:'):
            return 'kota bekliyor'
        return {'BLOCKED': 'durdu', 'AWAITING_USER': 'seni bekliyor', 'WAITING_LIMIT': 'limit bekliyor', 'IDLE': 'sırada',
                'WAITING_DEP': 'bağımlılık bekliyor', 'WAITING_ENV': 'ortam bekliyor',
                'WAITING_FOR_LEAD': 'Lead bekliyor', 'DISABLED': 'kapalı', 'MANUAL': 'manuel'}.get(t['status'], t['status'] or '-')

    def progress(self, t, width):
        path = t.get('path') or []
        if t.get('stage') == 'DONE' or not path:
            done = len(path) or 1
        else:
            done = path.index(t['stage']) if t['stage'] in path else 0
        n = round(done / max(1, len(path) or 1) * width)
        return '━' * n, '━' * (width - n)

    # ----- panes -----
    def header(self, w):
        x = 1
        title = '◆ AI ORCHESTRA'
        shift = self.frame() // 2
        for i, ch in enumerate(title):
            x = self.put(0, x, ch, self.grad[(i + shift) % len(self.grad)])
        x = self.put(0, x, f'  {self.root.name}', self.C['text'] | curses.A_BOLD)
        chips = [(' ● runner ' if self.online else ' ○ runner kapalı · s ', 'ok' if self.online else 'bad')]
        run = sum(t.get('status') == 'RUNNING' for t in self.rows)
        wait = sum(needs_user(t) for t in self.rows)
        if run:
            chips.append((f' {SPIN[self.frame() % 10]} {run} çalışıyor ', 'run'))
        if wait:
            chips.append((f' ⚑ {wait} seni bekliyor ', 'warn'))
        if self.paused:
            chips.append((' ⏸ duraklatıldı ', 'warn'))
        cx = w - sum(len(c) + 1 for c, _ in chips) - 7
        for c, k in chips:
            self.put(0, cx, c, self.chip[k]); cx += len(c) + 1
        self.put(0, w - 6, time.strftime('%H:%M' if self.frame() // 4 % 2 else '%H %M'), self.C['dim'])
        n, tok, usd = self.today()
        x = self.put(1, 1, 'bugün ', self.C['dim'])
        x = self.put(1, x, f'{n} tur · ${usd:.2f} · ', self.C['text'])
        self.tokens(1, x, tok, w - 1, 'today')

    def limits(self, y, w):
        """Subscription windows per engine, full width: the main reason work waits."""
        h = 4
        snaps = [(e, label, quota.current(e, self.quota), (self.quota.get(e) or {}).get('at')) for e, label in (('claude', 'Claude'), ('codex', 'Codex '))]
        held = [t for t in self.rows if t.get('status') == 'WAITING_LIMIT']
        busy = {'codex' if t.get('role') == 'verifier' else 'claude' for t in self.rows if t.get('status') == 'RUNNING'}
        self.box(y, 0, h, w - 1, 'Limitler', attr=self.C['warn'] if held else None,
                 right=f'{len(held)} görev limit bekliyor' if held else 'ölçüm token harcamaz')
        bar = max(6, (w - 96) // 2)
        reset_w = next((r for r in (22, 12) if 9 + 2 * (19 + bar + r) <= w - 3), 0)
        for i, (engine, label, cur, at) in enumerate(snaps):
            yy, x = y + 1 + i, 2
            x = self.put(yy, x, label + ' ', self.C['text'] | curses.A_BOLD)
            if not cur:
                self.put(yy, x, 'veri yok — ilk rol çalışınca gelir', self.C['dim']); continue
            for win, name in (('five_hour', '5 saat'), ('seven_day', 'hafta ')):
                c = cur.get(win)
                if not c:
                    continue
                u, reset = c['util'], c['resets_at']
                tone = self.C['bad'] if u >= 0.9 else self.C['warn'] if u >= 0.7 else self.C['ok']
                x = self.put(yy, x, f' {name} ', self.C['dim'])
                x = self.meter(yy, x, self.ease(engine + win, u), bar, tone, engine in busy)
                x = self.put(yy, x, f" %{self.ease(engine + win + '%', u * 100):<3.0f}", tone | curses.A_BOLD)
                at_ = datetime.fromtimestamp(reset) if reset else None
                clock = (('' if win == 'five_hour' else 'Pzt Sal Çar Per Cum Cmt Paz'.split()[at_.weekday()] + ' ') + at_.strftime('%H:%M')) if at_ else ''
                text = f'↻ {clock}' + (f' ({until(reset)})' if reset_w > 12 else '') if reset and u > 0 else ''
                x = self.put(yy, x, text[:reset_w].ljust(reset_w), self.C['dim'])
            if at and x + 14 <= w - 2:
                self.put(yy, w - 14, f'{since(datetime.fromtimestamp(at, timezone.utc).isoformat())} önce', self.C['faint'])
        return h

    def now(self, y, w):
        """What is happening right now: running roles with their latest step, else why tasks wait."""
        items = []
        for t in self.rows:
            if t.get('status') == 'RUNNING':
                log, lines = self.log_lines(t['id'])
                started = log.stat().st_birthtime if log and hasattr(log.stat(), 'st_birthtime') else None
                last = next((l for l in reversed(lines) if l.strip()), 'başlıyor…')
                e = int(time.time() - started) if started else None
                clock = f'{e // 3600}:{e % 3600 // 60:02d}:{e % 60:02d}' if e and e >= 3600 else f'{e // 60}:{e % 60:02d}' if e is not None else ''
                items.append(('run', t, f"{ROLE_TR.get(t['role'], t['role'] or '?')}" + (f' · {clock}' if clock else ''), last))
            elif t.get('status', '').startswith('WAITING_') and t.get('stage') not in team.CLOSED_STAGES:
                when = ''
                if t.get('retry_at'):
                    with contextlib.suppress(ValueError):
                        when = ' → ' + datetime.fromisoformat(t['retry_at']).astimezone().strftime('%H:%M')
                items.append(('wait', t, self.state_text(t) + when, t.get('message') or ''))
        items = sorted(items, key=lambda i: i[0] != 'run')[:4]
        h = 2 + max(1, len(items))
        n = len(self.grad)
        breath = self.grad[abs(self.frame() // 2 % (2 * n - 2) - (n - 1))]
        self.box(y, 0, h, w - 1, 'Şu an', attr=breath & ~curses.A_BOLD if any(i[0] == 'run' for i in items) else None)
        if not items:
            self.put(y + 1, 2, 'boşta — çalışan ya da bekleyen rol yok', self.C['dim'])
        for i, (kind, t, what, detail) in enumerate(items):
            x = self.put(y + 1 + i, 2, (SPIN[self.frame() % 10] if kind == 'run' else '◷') + ' ', self.C['run' if kind == 'run' else 'warn'] | curses.A_BOLD)
            x = self.put(y + 1 + i, x, f"{t['id']:<16}", self.C['text'] | curses.A_BOLD)
            x = self.put(y + 1 + i, x, f'{what:<22} ', self.C['run' if kind == 'run' else 'warn'])
            self.put(y + 1 + i, x, detail[:max(0, w - x - 3)], self.C['cmd'] if detail.startswith('$') else self.C['dim'])
        return h

    def task_list(self, y, x, h, w):
        self.box(y, x, h, w, 'Görevler', right=f'{len(self.rows)}')
        inner = h - 2
        if not self.rows:
            for i, l in enumerate(LOGO):
                self.put(y + 2 + i, x + 2, l[:w - 4], self.grad[(i * 2 + self.frame() // 3) % len(self.grad)])
            self.put(y + 5, x + 4, 'Görev yok. Soldaki Lead\'e ne yapılacağını yaz.', self.C['dim'])
            return
        top = max(0, self.sel - inner + 1)
        for i, t in enumerate(self.rows[top:top + inner]):
            yy, me = y + 1 + i, top + i == self.sel
            if me:
                self.put(yy, x + 1, ' ' * (w - 2), self.chip['key'])
            base = self.chip['key'] if me else 0
            g, ga = self.glyph(t)
            fl = self.flash.get(t['id'], -1e9)
            if t.get('stage') == 'DONE' and time.monotonic() - fl < 4:
                g, ga = SPARKLE[self.frame() % len(SPARKLE)], self.C['ok'] | curses.A_BOLD
            cx = self.put(yy, x + 1, '▌' if me else ' ', self.C['acc'] | curses.A_BOLD)
            cx = self.put(yy, cx + 1, g, ga | (base & ~curses.A_BOLD))
            cx = self.put(yy, cx + 1, f"{t['id']:<12}", base | curses.A_BOLD if me else self.C['text'])
            a, b = self.progress(t, 8)
            pc = self.C['ok'] if t.get('stage') == 'DONE' else self.C['run'] if t.get('status') == 'RUNNING' else self.C['acc']
            bx = self.put(yy, cx, a, pc); cx = self.put(yy, bx, b, self.C['faint'])
            if t.get('status') == 'RUNNING' and b:
                self.put(yy, bx + self.frame() // 2 % len(b), '━', self.C['run'] | curses.A_BOLD)
            st = self.state_text(t)[:20]
            cx = self.put(yy, cx + 1, f'{st:<20}', ga & ~curses.A_BOLD if not me else ga)
            cx = self.put(yy, cx, f"{since(t.get('updated')):>5} ", self.C['dim'])
            room = x + w - 2 - cx
            if room > 4:
                self.put(yy, cx, (t.get('title') or '')[:room], (base if me else self.C['dim']))
            self.sweep(yy, x + 1, w - 2, fl, self.chip['ok' if t.get('stage') == 'DONE' else 'run'])

    def pipeline(self, t, y, x, w):
        path = t.get('path') or []
        cur = path.index(t['stage']) if t.get('stage') in path else (len(path) if t.get('stage') == 'DONE' else -1)
        seg = max(3, min(8, (w - 4 - sum(len(SHORT.get(s, s)) + 2 for s in path)) // max(1, len(path) - 1)))
        cx = x
        for i, s in enumerate(path):
            if i:
                line = '─' * seg
                if i <= cur:
                    cx = self.put(y, cx, line, self.C['ok'])
                elif i == cur + 1 and t.get('status') == 'RUNNING':
                    k = self.frame() % seg
                    cx = self.put(y, cx, '─' * k + '•' + '─' * (seg - k - 1), self.C['faint'])
                else:
                    cx = self.put(y, cx, line, self.C['faint'])
            if i < cur or t.get('stage') == 'DONE':
                dot, a = '●', self.C['ok']
            elif i == cur:
                dot = '◉' if self.frame() // 4 % 2 or t.get('status') != 'RUNNING' else '○'
                a = (self.C['run'] if t.get('status') == 'RUNNING' else self.C['warn'] if needs_user(t) else self.C['acc']) | curses.A_BOLD
            else:
                dot, a = '○', self.C['faint']
            cx = self.put(y, cx, f'{dot} ', a)
            cx = self.put(y, cx, SHORT.get(s, s), a if i <= cur else self.C['dim'])

    def detail(self, t, y, x, h, w):
        rs = runs(self.metrics, t['id'])
        fresh, cache = sum(r['fresh'] for r in rs), sum(r['cache'] for r in rs)
        meta = ' · '.join(filter(None, [t.get('profile'), t.get('worktree') and '⎇ ' + t['worktree'], t.get('rework') and f"rework {t['rework']}"]))
        meta = meta[:max(0, w // 3)]
        self.box(y, x, h, w, f"{t['id']} · {(t.get('title') or '')[:max(10, w - len(meta) - 24)]}", attr=self.C['acc'] if needs_user(t) else None, right=meta)
        self.pipeline(t, y + 2, x + 3, w - 6)
        g, ga = self.glyph(t)
        cx = self.put(y + 4, x + 3, g + ' ', ga)
        cx = self.put(y + 4, cx, self.state_text(t), ga | curses.A_BOLD)
        if t.get('message'):
            self.put(y + 4, cx + 2, '— ' + t['message'][:w - (cx - x) - 6], self.C['bad'] if needs_user(t) else self.C['dim'])
        if rs:
            cx = self.put(y + 5, x + 3, f"{len(rs)} tur · {sum(r['sec'] for r in rs) / 60:.0f} dk · ${sum(r['usd'] for r in rs):.2f} · ", self.C['text'])
            self.tokens(y + 5, cx, token_sum(rs), x + w - 2, t['id'])
        top = y + 7
        self.put(top - 1, x + 1, '─' * (w - 2), self.C['faint'])
        room = h - (top - y) - 1
        if self.info:
            self.timeline(t, rs, top, x + 3, room, w - 6)
        else:
            self.log(t, top, x + 3, room, w - 6)

    def timeline(self, t, rs, y, x, h, w):
        self.put(y - 1, x, ' rol zaman çizelgesi ', self.C['acc'] | curses.A_BOLD)
        gates = t.get('gates') or []
        top = max([r['fresh'] for r in rs] or [1]) or 1
        model_w = 14 if w >= 90 else 0
        bar_w = max(6, w - 85 + 14 - model_w)
        for i, r in enumerate(rs[-(h - len(gates)):]):
            ok = r['result'] in ('advanced', 'decided')
            bad = r['result'] in ('crash', 'blocked', 'cancelled')
            c = self.C['ok'] if ok else self.C['bad'] if bad else self.C['warn']
            cx = self.put(y + i, x, f"{'✓' if ok else '✗' if bad else '↺'} ", c)
            cx = self.put(y + i, cx, f"{ROLE_TR.get(r['role'], r['role'] or '?'):<10}", self.C['text'] | curses.A_BOLD)
            cx = self.put(y + i, cx, f"{r['model'][:13]:<14}"[:model_w], self.C['dim'])
            n = max(1, round(r['fresh'] / top * bar_w))
            cx = self.put(y + i, cx, '█' * n, self.C['hot']); cx = self.put(y + i, cx, '░' * (bar_w - n), self.C['faint'])
            q = r['quota']
            used = f" · {q.get('engine', '')} 5s %{q['five_hour'] * 100:.0f}" if isinstance(q.get('five_hour'), (int, float)) else ''
            self.put(y + i, cx + 1, f"{human(r['fresh']):>6} tam · {human(r['cache']):>5} önb · {r['sec'] / 60:4.1f}dk{used}  {r['result']}", c, x + w)
        for j, g in enumerate(gates):
            self.put(y + h - len(gates) + j, x, f"⚑ {g.get('kind')}: {g.get('action')} → {g.get('target')}  (Lead'e onay ver)", self.C['warn'] | curses.A_BOLD)
        if not rs and not gates:
            self.put(y, x, 'henüz rol turu yok', self.C['dim'])

    def log(self, t, y, x, h, w):
        log, lines = self.log_lines(t['id'])
        live = t.get('status') == 'RUNNING'
        self.put(y - 1, x, f" {'● canlı' if live else 'günlük'} · {log.name if log else 'yok'} ", (self.C['run'] if live else self.C['dim']) | curses.A_BOLD)
        wrapped = []
        for l in lines:
            first = True
            while l:
                n = w if first else w - 2
                wrapped.append((l[:n], first)); l = l[n:]; first = False
        for i, (l, first) in enumerate(wrapped[-h:]):
            c = self.C['cmd'] if l.startswith('$') else self.C['bad'] if l.startswith('!') else self.C['text'] if l.startswith('›') else self.C['dim']
            self.put(y + i, x if first else x + 2, l, c)
        if live:
            self.put(y + min(h - 1, len(wrapped[-h:])), x, SPIN[self.frame() % 10] + ' ', self.C['run'])

    def footer(self, h, w):
        if self.note:
            self.put(h - 1, 0, f' {self.note} '.ljust(w - 1), self.chip['warn']); return
        x = 1
        ev = self.event
        if ev and time.monotonic() - ev[0] < 8:
            text = f' {ev[1]} '
            slide = max(0, int((1 - (time.monotonic() - ev[0]) * 4) * len(text)))
            self.put(h - 1, w - 1 - len(text) + slide, text, self.chip[ev[2]])
            w -= len(text) + 1
        for k, label in [('↑↓', 'seç'), ('i', 'zaman/log'), ('r', 'yanıt'), ('a', 'devam'), ('c', 'iptal'), ('s', 'runner'), ('p', 'duraklat'), ('q', 'çık')]:
            if x + len(k) + len(label) + 4 >= w:
                break
            x = self.put(h - 1, x, f' {k} ', self.chip['acc']); x = self.put(h - 1, x, f' {label}  ', self.C['dim'])

    def draw(self):
        self.refresh_data()
        scr = self.scr; scr.erase(); h, w = scr.getmaxyx()
        if h < 16 or w < 60:
            self.put(0, 0, 'Pencere küçük — büyüt', self.C['warn']); scr.refresh(); return
        p = (time.monotonic() - self.born) / 0.9
        if p < 1:  # intro: the logo wipes in left to right with a moving gradient; any key skips it
            lw = max(map(len, LOGO))
            for i, l in enumerate(LOGO):
                for j, ch in enumerate(l[:int(p * 1.3 * lw)]):
                    self.put(h // 2 - 2 + i, (w - lw) // 2 + j, ch, self.grad[(j // 3 + self.frame()) % len(self.grad)])
            self.put(h // 2 + 1, (w - len(self.root.name)) // 2, self.root.name, self.C['dim'])
            scr.refresh(); return
        self.header(w)
        self.sel = min(self.sel, max(0, len(self.rows) - 1))
        body_y = 2 + self.limits(2, w)
        if h >= 28:
            body_y += self.now(body_y, w)
        body_h = h - body_y - 1
        if w >= 140:  # wide: list left, detail right
            lw = 66
            self.task_list(body_y, 0, body_h, lw)
            if self.rows:
                self.detail(self.rows[self.sel], body_y, lw, body_h, w - lw - 1)
        else:
            lh = min(len(self.rows), max(3, body_h // 3)) + 2 if self.rows else 8
            self.task_list(body_y, 0, lh, w - 1)
            if self.rows and body_h - lh >= 9:
                self.detail(self.rows[self.sel], body_y + lh, 0, body_h - lh, w - 1)
        self.footer(h, w)
        scr.refresh()

    def ask(self, prompt):
        """One-line UTF-8 input on the footer; Esc cancels."""
        h, w = self.scr.getmaxyx(); buf = ''
        curses.curs_set(1); self.scr.timeout(-1)
        try:
            while True:
                self.put(h - 1, 0, (' ' + prompt + buf).ljust(w - 1), self.chip['acc'])
                self.scr.move(h - 1, min(w - 2, len(prompt + buf) + 1))
                ch = self.scr.get_wch()
                if ch in ('\n', '\r', curses.KEY_ENTER):
                    return buf.strip()
                if ch == '\x1b':
                    return ''
                if ch in ('\x7f', '\b', curses.KEY_BACKSPACE):
                    buf = buf[:-1]
                elif isinstance(ch, str) and ch.isprintable():
                    buf += ch
        finally:
            curses.curs_set(0); self.scr.timeout(120)

    def act(self, key):
        t = self.rows[self.sel] if self.rows else None
        try:
            if key in (curses.KEY_DOWN, 'j'):
                self.sel = min(self.sel + 1, len(self.rows) - 1)
            elif key in (curses.KEY_UP, 'k'):
                self.sel = max(self.sel - 1, 0)
            elif key in ('i', '\t'):
                self.info = not self.info
            elif key == 's':
                self.note = 'Runner ' + orchestra.start_runner(self.root)
            elif key == 'p':
                f = team.config_dir() / 'paused'
                if f.exists():
                    f.unlink(); self.note = 'Yeni görevler devam ediyor'
                else:
                    f.parent.mkdir(parents=True, exist_ok=True); f.write_text(team.now())
                    self.note = 'Duraklatıldı: yeni rol başlamaz, çalışan sürer'
            elif t and key == 'r':
                text = self.ask(f'{t["id"]} yanıt/not (Esc iptal): ')
                if text:
                    team.reply(self.root, t['id'], text); self.note = 'İletildi; durmuşsa Lead değerlendirip sürdürür'
            elif t and key == 'a':
                orchestra.cli(self.root, 'automate', t['id'], '--resume'); self.note = t['id'] + ' devam ediyor'
            elif t and key == 'c':
                if self.ask(f'{t["id"]} çalışan rol iptal edilsin mi? (evet yaz): ').lower() == 'evet':
                    self.note = orchestra.cancel(self.root, t['id'])
            else:
                return
        except Exception as e:  # show, don't crash the board
            self.note = 'Hata: ' + str(e)
        self.loaded = 0

    def loop(self):
        note_at = 0
        while True:
            self.draw()
            try:
                key = self.scr.get_wch()
            except curses.error:
                if self.note and time.monotonic() - note_at > 4:
                    self.note = ''
                continue
            self.born = 0
            if key == 'q':
                return
            if key != curses.KEY_RESIZE:
                self.note = ''
                self.act(key); note_at = time.monotonic()


def board(name):
    _, root = resolve(name)
    locale.setlocale(locale.LC_ALL, '')
    os.environ.setdefault('ESCDELAY', '25')
    os.environ.setdefault('TERM', 'xterm-256color')
    curses.wrapper(lambda scr: Board(scr, root).loop())


def main(argv):
    if argv and argv[0] in ('-h', '--help'):
        print(__doc__.strip()); print('\nProjeler:', ', '.join(orchestra.registry()) or '-'); return
    if argv[0] == 'board':
        return board(argv[1] if len(argv) > 1 else '.')
    launch(argv[0] if argv else '.')


if __name__ == '__main__':
    main(sys.argv[1:])
