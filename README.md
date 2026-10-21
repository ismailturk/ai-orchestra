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
git clone https://github.com/ismailturk/ai-orchestra.git ~/ai-orchestra
cd ~/ai-orchestra && ./install.sh          # --claude-rules: çekirdek kuralları ~/.claude/CLAUDE.md sonuna ekler
team doctor
```
`team` ve `team-runner` komutları `~/.local/bin` altına kurulur. Güncelleme: `team update` (yalnız `git pull --ff-only`).

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

## Yapılandırma (proje içinde, hepsi isteğe bağlı)
- `.ai/models.json`: rol -> model (`developer_small` tweak/small için).
- `.ai/runner.json`: `max_parallel`, `serial_roles`; ayrıca `role_prompt_extra` (tüm rol istemlerine eklenen projeye özel kurallar), `codex_model`, `env` ve `env_by_role` (rol süreçlerine ek ortam değişkenleri).
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
python3 bin/test_team.py && python3 bin/test_team_runner.py && python3 bin/test_e2e.py   # e2e: sahte claude/codex, ağ yok
```

## Lisans
Henüz belirlenmedi.
