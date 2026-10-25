# ai-orchestra

Claude Code ve Codex ile rol tabanlı, dosya tabanlı yazılım geliştirme orkestrasyonu. Sohbet değil, proje içindeki doğrulanmış dosyalar kalıcı hafızadır; roller görevleri bu dosyalar üzerinden devralır.

*English summary:* ai-orchestra is a small Python toolkit (no dependencies beyond Python 3.10+) that orchestrates Claude Code and Codex as fixed roles (Lead, Analyst, Designer, Developer, Tester, Verifier) over a file-based task workflow. Tasks move through stages (analysis, design, implementation, independent tests, knowledge update, done) with fingerprinted evidence gates, a parallel runner with scoped file locks, automatic waiting on rate limits, and mandatory human gates for DB changes, deployments, pushes and other high-impact actions. License: not decided yet.

## Ne yapar
- **Roller:** Lead (tek kullanıcı teması, kod yazmaz), Analyst, Designer, Developer (tek kaynak yazarı, kilitli), Tester, Verifier/Reporter (bağımsız ikinci doğrulama; Codex veya `TEAM_VERIFIER_ENGINE=claude`).
- **Aşamalar ve profiller:** `fix`, `tweak`, `small`, `ui`, `full`, `risky`. Aşamalar: NEW, ANALYSIS, DESIGN, IMPLEMENTATION, TEST_CLAUDE, VERIFY_CODEX, KNOWLEDGE_UPDATE, DONE.
- **Kanıt kapıları:** test sonuçları kaynak parmak izine bağlıdır; kaynak değişirse eski kanıt geçersizdir. Test PASS ve bağımsız doğrulama PASS olmadan DONE olmaz.
- **Paralel runner:** `team-runner` `--auto` görevleri sırayla/paralel yürütür; TASK.md `## Dosyalar` kapsamı kesişmeyen görevler ayrı kilitle aynı anda çalışır.
- **Limit bekleme:** limit/ağ hatasında görev `WAITING_LIMIT` olur ve `retry_at`'te aynı aşamadan sürer.
- **İnsan kapıları:** DB, deployment, push/merge, gerçek bildirim/e-posta, toplu/geri dönüşü zor işlemler kullanıcı onayı olmadan çalıştırılmaz (bkz. `TEAM_RULES.md`).

## Gereksinimler
Python >= 3.10, git, [Claude Code](https://claude.com/claude-code) CLI (`claude`); Verifier için isteğe bağlı Codex CLI (`codex`). cmux paneli (`open_workspaces.py`) isteğe bağlıdır.

## Kurulum
```
git clone https://github.com/ismailturk/ai-orchestra.git ~/ai-orchestra   # kendi özel kopyan varsa onun adresi
cd ~/ai-orchestra && ./install.sh          # --claude-rules: çekirdek kuralları ~/.claude/CLAUDE.md sonuna ekler
team doctor
```
`team`, `team-runner`, `team-tui` komutları `~/.local/bin` altına kurulur. Güncelleme: `team update` (yalnız `git pull --ff-only`).

## Başka bilgisayarda, başka bir projede kullanım (adım adım)
Komutlar macOS içindir; Linux'ta servis systemd ile kurulur (aşağıda).

1. **Araçlar:** Python ≥ 3.10, git, Node.js (Codex için).
   ```
   npm i -g @anthropic-ai/claude-code @openai/codex   # Claude Code + Codex CLI
   claude          # bir kez aç, hesabınla giriş yap, çık
   codex login     # Verifier için (ChatGPT hesabı)
   ```
2. **ai-orchestra'yı kur:** yukarıdaki *Kurulum* (`git clone … && ./install.sh && team doctor`). `~/.local/bin` PATH'te değilse install.sh uyarır; `~/.zshrc`'ye ekle.
3. **Projeyi uygun yere koy:** `~/Projects/<proje>` gibi. **`~/Desktop`, `~/Documents` altına koyma:** macOS izinleri (TCC) arka plan servisinin oradaki dosyalara erişmesini engeller, servis "exit 78" ile açılmaz.
4. **Projeye ekibi kur:** cmux'ta proje klasörüne geç ve `team-tui` yaz. Kurulum yoksa Claude açılır; `bu projede gerekli kurulumları yap` de (ya da `/orchestra-setup`). Bitince `/exit` ve tekrar `team-tui`: Lead, pano ve runner açılır. Beceri `team init` ile `.ai/`, `roles/`, `tasks/`, `AGENTS.md`, `CLAUDE.md`, `.ai/models.json`, `.ai/runner.json` oluşturur, projeyi salt okunur inceler, belgeleri doldurur ve yalnız yetki sorularını sorar (DB kimde, deploy/push kimde, Codex Verifier olsun mu). Commit/push yapmaz.
5. **Proje kaydı:** kurulum projeyi `team register` ile `~/.config/ai-team/projects.json`'a kendisi ekler (ad = klasör adı); sonra her yerden `team-tui <ad>`. Elle: `team register [ad]`.
6. **Ayarları gözden geçir** (`.ai/` altında, hepsi isteğe bağlı):
   - `models.json`: Developer varsayılanı her profilde Opus 5.5; diğer roller Sonnet.
   - `runner.json`: `codex_model` (ör. `gpt-6.1-sol`, Codex CLI ≥ 0.159.1), `codex_args` (öneri: `["--disable","plugins","--disable","apps","-c","agents.enabled=false"]`), `preflight` (ör. Docker gerektiren testler için `{"developer":["docker info"],"tester":["docker info"],"verifier":["docker info"]}`), `role_prompt_extra` (projeye özel test kapsamı/kurallar).
7. **Kalıcı servis (önerilir):** runner arka planda, oturum açılınca başlar, çökerse yeniden açılır.
   ```
   team-runner install-agent --project ~/Projects/<proje>
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-orchestra.runner.<ad>.plist
   launchctl list | grep ai-orchestra        # çalışıyor mu
   ```
   Yeni sürümden (team update) sonra (çalışan rol yokken) yeniden başlat: `launchctl kickstart -k gui/$(id -u)/com.ai-orchestra.runner.<ad>`. Kaldır: `team-runner uninstall-agent --project … --apply`. Linux: `install-agent` `~/.config/systemd/user/` altına birim yazar ve `systemctl --user` komutunu yazdırır.
8. **Çalış:** `team-tui <ad>`: solda Lead (Claude) ile konuşursun, sağda pano (limitler, şu an çalışan rol, görevler). Görevi Lead açar; runner rolleri sırayla yürütür; insan kapıları (DB, push, deploy) sende kalır.

**Bilgisayarlar arasında ne taşınır, ne taşınmaz:**
- Proje deposunda commit'li olanlar (kod, `roles/`, ve git'te tutuyorsan `.ai/` belgeleri, `tasks/`) diğer bilgisayara gelir. Bir projede `.ai/` ya da `tasks/` git dışındaysa (`.git/info/exclude`) proje hafızası ve görev geçmişi o bilgisayarda kalır.
- Makineye özel olanlar taşınmaz ve taşınmamalı: `~/.config/ai-team/` (proje listesi, limit ölçümleri), Claude/Codex girişleri, servis dosyaları. Her bilgisayarda 1, 2, 5 ve 7. adımlar bir kez yapılır.
- Aynı projeyi iki bilgisayarda **aynı anda** runner ile yürütme: görev durumu dosya tabanlıdır, iki runner birbirini görmez.
- Limitler hesaba bağlıdır: iki bilgisayarda aynı Claude/Codex hesabı aynı 5 saat/haftalık pencereyi tüketir.

## Yeni projede kurulum
```
cd /yol/proje && claude
> bu projede gerekli kurulumları yap        # ya da /orchestra-setup
```
`orchestra-setup` becerisi (install.sh kurar) `team doctor`, `team init`, salt okunur proje incelemesi ve yetki sorularını yürütür; DB/deploy/push yapmaz, commit atmaz. Elle yol:
```
cd /yol/proje
team init --dry-run   # ne oluşacağını göster
team init             # .ai/*.md, .ai/messages/<rol>/, roles/*.md, tasks/, CLAUDE.md, AGENTS.md, models.json, runner.json; var olan dosyaya dokunmaz
```
Mevcut `CLAUDE.md` değiştirilmez; ai-orchestra satırını elle eklemen için uyarı basılır. Ardından `.ai/PROJECT.md`, `OWNERSHIP.md`, `CURRENT_STATE.md` doldurulur (bkz. `ONBOARDING.md`). İstersen proje yollarını `~/.config/ai-team/projects.json` içine yaz (örnek: `projects.example.json`). Projeye özel istisnalar yalnız projedeki `.ai/TEAM_RULES.md` içinde olur; yoksa bu depodaki `TEAM_RULES.md` yedek olarak okunur.

## Temel komutlar
```
team new ID "Kısa başlık" --profile small --auto
team status [ID]            team inbox lead --task ID
team advance ID STAGE       team claim ID --session S / team release ID
team evidence ID --role tester|verifier --result PASS|FAIL|BLOCKED --session S --note "..."
team rework ID --reason "..."   team reclassify ID PROFIL --reason "..."
team knowledge ID --summary "..."   team gate ID --kind DB|HIGH_IMPACT|UNKNOWN_IMPACT ...
team automate ID --resume|--disable
team close ID --reason "..." [--abandon]     # CLOSED / ABANDONED: tam akış olmadan kapat
team commit ID [--apply] [--force]           # görev başı commit taslağı; varsayılan DRY-RUN, push yok
team status [--all]                          # gruplu özet; --all eski tam JSON
team report [--days N] [--task ID]           # süre, token/maliyet, rework, müdahale, bağımsızlık
team metrics backfill                        # eski runner log'larından metrics.jsonl doldur (idempotent)
team dashboard [--project P ...|--all] [--out yol] [--open] [--serve PORT]
team budget [--resume [--task ID]]
team-runner --project /yol/proje watch | status
team-runner install-agent --project /yol/proje [--dry-run]   # launchd/systemd dosyası yazar, komutu yazdırır
team-runner uninstall-agent --project /yol/proje [--apply]
```
Tüm komutlar `--project <yol>` ile veya proje klasöründen çalışır; ayrıntı için `team --help`.

## Terminal (varsayılan): team-tui

`team-tui [proje]` (cmux içinde): runner'ı başlatır, sağda canlı panoyu açar, solda interaktif `claude` Lead olur. Proje verilmezse bulunduğun klasör; kurulu değilse kurulum için Claude açılır.
Lead ile anlık konuşursun; Lead görevi kapsamıyla kendisi açar (runner'ın ayrı Lead girişi atlanır).
Pano: `↑↓` seç · `i` rol zaman çizelgesi/token ↔ canlı log · `r` yanıt · `a` devam · `c` iptal · `s` runner · `p` duraklat · `q` çık.
Yalnız pano: `team-tui board <proje>`. Web arayüzü ikincildir.

## AI Command Center (Personal V1)
```
./install.sh --menubar          # team-orchestra + team-memory komutları; menü çubuğu uygulaması ~/Applications/AI Orchestra.app
team-orchestra --open           # http://127.0.0.1:8765 (yalnız 127.0.0.1; token ~/.config/ai-team/token, 0600)
open ~/Applications/"AI Orchestra.app"
```
- **Görev ver:** proje seç, doğal dille yaz, gönder. Gerçek `team new --intake` görevi açılır; kural tabanlı profil (`micro=fix`, `small`, `standard`, `complex=full`, `critical=risky`) ve en fazla 3 skill önerisi kaydedilir; runner yoksa başlatılır. Runner NEW aşamasında **Lead'i gerçekten çalıştırır** (complex/critical: Opus): TASK.md'yi kapsam/kabul kriterleri/`## Dosyalar` ile doldurur ve ilk aşamaya ilerletir. Emin değilse TASK.md'ye `## Lead sorusu` yazar; görev BLOCKED olur, arayüzden **Yanıtla** (`team reply ID --text`) ile sürer.
- **İzole worktree (varsayılan açık):** görev `ai/<ID>` dalında `~/.local/share/ai-team/worktrees/<proje>/<ID>` altında çalışır; kullanıcının dalı ve commit edilmemiş işi değişmez. Kanıt parmak izi worktree'den alınır; `team commit ID --apply` yalnız o dala commit eder (Tester testleri dahil, kopyalanan `.claude/settings*.json`/AGENTS.md hariç). Merge/push yok. Gerekli ignore'lu dosyalar: `.ai/runner.json` → `"worktree_copy": ["android/local.properties"]`.
- **Güvenlik katmanı:** Claude rollerine `--disallowedTools` (git push/merge/rebase/reset --hard, DB/ef database, publish/deploy, sudo, rm -rf); iki motora da `GIT_CONFIG_*` ile ağ push URL'leri geçersiz şemaya çevrilir (git push git içinde başarısız olur). API: token + Host + Origin + JSON kontrolü; GET uçları hiçbir şey yazmaz; günlükler gösterilirken sırlar maskelenir.
- **Kurtarma/sınırlar:** runner kilidi alınca yarım kalan RUNNING görevler yeniden sıraya alınır (ölü turun kilidi bırakılır, kanıtlar parmak iziyle yeniden kontrol edilir); RUNNING görev ikinci kez başlatılmaz. `.ai/runner.json` → `"limits": {"max_reworks": 3, "block_repeated_failure": true, "task_minutes": null, "role_minutes": null}`. Aynı rework nedeni art arda gelirse görev BLOCKED. Rol durdurma süreç grubunu kapatır.
- **Pause/İptal:** Pause (`~/.config/ai-team/paused`) yalnız yeni rol başlatmayı durdurur; çalışan rol sürer. Aktif rolü iptal etmek görev ekranında ayrı ve onaylıdır.
- **Project Memory:** `team-memory --project P index|search Q|summary|fact user_decision "..."` → `.ai/memory.sqlite` (SQLite FTS5, sha tabanlı artımlı, git sürümü). `.env`, anahtar/sertifika, credentials, `.claude/` `.codex/` oturumları indekslenmez; içerik maskelenir. Kayıt türleri: kaynaktan doğrulanan / kullanıcı kararı / doğrulanmamış çıkarım; kaynağı değişen kayıt `stale` olur.
- **Context Shadow Mode:** her rol turunda önerilen bağlam paketi `.ai/context-shadow/<ID>/` altına yazılır; gerçek istem/komut değişmez (testle doğrulanır). `"context_shadow": false` kapatır.
- **Token:** görev/rol/model başına token, süre, ilk deneme başarısı, rework, limit takılması; maliyet "API eşdeğeri" olarak gösterilir, abonelikte fatura değildir.

## Kota ve kesintiler (hesap değiştirmeden)
- **Canlı kota:** Claude rolleri akışta `rate_limit_event` (5 saat/7 gün kullanımı), Codex rollout dosyaları `rate_limits` yazar; runner bunları token harcamadan `~/.config/ai-team/quota.json`'a (makinedeki tüm runner'lar ortak) kaydeder. Claude rolü çalışmıyorken (senin interaktif oturumların da pencereyi doldurur) Claude anlık görüntüsü 10 dk'dan eskiyse runner `claude -p /usage` ile tazeler: model çağrısı yok (ölçüldü: 0 token, $0, ~4 sn). Pano başlığı iki motorun 5s/7g doluluğunu ve sıfırlanma saatini gösterir.
- **Kabul:** rol başlamadan önce tipik maliyeti (bu makinede ölçülen son 20 turun medyanı; 5 turdan azsa ölçülmüş varsayılan) pencereye ve çalışan rollerin kalan payına eklenir. Sığmıyorsa rol başlamaz, görev `WAITING_LIMIT` ("Kota: …") olur ve sıfırlanmadan 1 dk sonra başlar. Claude 5s penceresinin %5'i interaktif Lead sohbeti için boş tutulur. Ayar: `.ai/runner.json` → `"quota": {"enabled": true, "reserve": {"claude": {"five_hour": 0.05}}}`.
- **Önce bitir:** runner işi DONE'a en yakın görevden başlatır; kıt kotada yarım iş birikmez.
- **Kaldığı yerden sürdürme:** kullanım limiti (5s veya haftalık) ya da ağ hatası rolü yarıda keserse runner aynı oturumu (`claude --resume` / `codex exec resume`) motorun bildirdiği sıfırlanmadan 2 dk sonra "DEVAM TURU" istemiyle sürdürür; iş baştan yapılmaz. En fazla 3 kez, sonra temiz oturum. Ağ hatası 2→5→10→20 dk geri çekilir. Rol çalışırken Mac'in boşta uykusu `caffeinate` ile engellenir (kapak kapanırsa Mac yine uyur).
- **Ortam ön kontrolü:** `.ai/runner.json` → `"preflight": {"tester": ["docker info"], "*": [...]}`; komut başarısızsa rol başlamaz, görev `WAITING_ENV` olur, düzelince kendiliğinden başlar (dakikada bir denenir, token harcamaz).
- **Bağımlılık:** TASK.md `## Bağımlılıklar` altındaki görev ID'leri kapanmadan iş aşaması başlamaz (`WAITING_DEP`).
- **Parmak izi koruması:** worktree'siz bir görev test/doğrulamadayken, kapsamı çakışan başka görevin Developer'ı başlamaz (Tester PASS'i geçersiz kalmasın).
- **Codex doğrulama maliyeti:** `.ai/runner.json` → `"codex_args": [...]` her Codex çağrısına eklenir (varsayılan yok; kullanıcı ayarı geçerli). Örnek: `["-c", "model_reasoning_effort=\"high\""]` (OpenAI'nin gözden geçirici önerisi), `["--disable", "plugins", "--disable", "apps", "-c", "agents.enabled=false"]` (eklenti/uygulama/alt ajan talimatı yok; istem girdisi 11,7K → 9,6K token, ölçüldü: CLI 0.164.0). Model için `codex_model`; `gpt-6.1-sol` Codex CLI ≥0.159.1 ister.

## Yapılandırma (proje içinde, hepsi isteğe bağlı)
- `.ai/models.json`: rol -> model (Developer varsayılanı her profilde `claude-opus-5-5`; isteğe bağlı `developer_small`).
- `.ai/runner.json`: `max_parallel`, `serial_roles`; ayrıca `role_prompt_extra` (tüm rol istemlerine eklenen projeye özel kurallar), `codex_model`, `codex_args`, `env` ve `env_by_role` (rol süreçlerine ek ortam değişkenleri).
- `.ai/runner.json` yeni anahtarlar: `risk_paths` (glob listesi; `fix` görevi bunlara dokunursa `small` akışına yükselir), `require_independent_verifier` (true: `fix` dışında tek motorlu PASS/PASS DONE olmaz), `budget` (`daily_usd`, `weekly_usd`, `task_usd`, `action`: `warn`|`pause`; null = kapalı).
- TASK.md `## Dosyalar`: satır başına bir glob; kanıt/parmak izi yalnız bu dosyalardan hesaplanır, kesişmeyen görevler paralel çalışır. Bölüm yoksa görev tüm repoyu kapsar.

## Hızlı düzeltme, commit, ölçüm
- **`fix` profili:** NEW → IMPLEMENTATION → TEST_CLAUDE → DONE. Yalnız Tester PASS ister; Lead/Verifier çalışmaz, DONE'da `.ai/CHANGELOG.md`'ye tek satır yazılır. Değişen dosya `risk_paths`'e değerse görev kendiliğinden `small` olur ve VERIFY_CODEX'ten geçer (`escalated`). `team new ... --profile auto [--files GLOB ...]` (veya `team reclassify ID auto`) kapsama göre `fix`/`small`/`ui` önerir ve nedenini yazar.
- **Görev başı commit:** runner her Developer çalışmasında (ve elle `claim`→`release` arasında) değişen dosyaları `changed_files`'a biriktirir (`.ai/`, `tasks/`, `roles/` hariç; kapsam varsa yalnız kapsam). `team commit ID` dosyaları, açık görev çakışmalarını, önerilen mesajı ve git komutlarını gösterir; `--apply` yalnız o dosyaları commit eder (başka staged değişiklik varsa durur), push etmez.
- **Bağımsızlık:** kanıt kaydı gerçek motoru (`engine_id`: claude|codex|other) ve modeli tutar. Kapanışta `PASS/PASS (bağımsız)`, `PASS (tek model)` ya da (eski kayıtta motor metrics'ten de bulunamazsa) `PASS (bağımsızlık bilinmiyor)` yazılır.
- **Kapsam dışı değişiklik:** Developer'ın `## Dosyalar` dışına yazdığı dosyalar `out_of_scope_changes`'a girer, `team status` ve runner log'u uyarır, `fix` risk kontrolünde sayılır. Git yoksa ve `risk_paths` tanımlıysa `fix` her zaman yükselir.
- **Parmak izi:** kanıt parmak izi araç dosyasının baytlarını değil `team.py` içindeki `GATE_VERSION`'ı içerir; araç güncellemesi açık kanıtları bozmaz, yalnız kapı anlamı değişince sürüm elle artırılır.
- **Ölçüm:** runner her rol çalışması için `.ai/metrics.jsonl`'e süre, sonuç, token (input/output/cache read/cache write) ve Claude maliyetini yazar; elle müdahaleler `type: intervention`. Codex log'u token verir, maliyet vermez. `team dashboard` dış kaynaksız tek HTML üretir (varsayılan `.ai/dashboard.html`); `--serve` yalnız 127.0.0.1'de her istekte yeniden üretir.
- **Bütçe:** `warn` %80/%100'de runner log'una ve `team status` başına uyarı yazar; `pause` %100'de yeni rol başlatmaz (çalışanı kesmez), `team budget --resume` o günü açar. Görev bütçesi aşılırsa görev `AWAITING_USER` olur (`team budget --resume --task ID`).
- **Tek runner:** `watch` kilit doluysa çıkmaz, 60 sn arayla bekler (tek log satırı).
- **Arka plan runner:** `install-agent` macOS'ta `~/Library/LaunchAgents/com.ai-orchestra.runner.<id>.plist`, Linux'ta systemd user unit yazar; `launchctl`/`systemctl` komutunu sen çalıştırırsın.
- **.gitignore önerisi:** `.ai/runner-logs/ .ai/metrics.jsonl .ai/dashboard.html .ai/budget.json .ai/.team.lock .ai/.runner.lock` (`team init` eksikleri yazdırır).

## Güvenlik notu
Markdown kuralları ve rol etiketleri bir işletim sistemi güvenlik duvarı değildir. Standart araç izinlerini/sandbox'ı koru, bypass kullanma; üretim erişimini ayrıca dar hesap/test altyapısıyla sınırla. DB, deployment, push ve merge işlemlerini insan onaylar ve uygular.

## Testler
```
python3 bin/test_team.py && python3 bin/test_team_runner.py && python3 bin/test_e2e.py && python3 bin/test_orchestra.py   # sahte claude/codex, ağ yok
```

## Lisans
Henüz belirlenmedi.
