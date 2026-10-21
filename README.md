# ai-orchestra

Claude Code ve Codex ile rol tabanlı, dosya tabanlı yazılım geliştirme orkestrasyonu. Sohbet değil, proje içindeki doğrulanmış dosyalar kalıcı hafızadır; roller görevleri bu dosyalar üzerinden devralır.

*English summary:* ai-orchestra is a small Python toolkit (no dependencies beyond Python 3.10+) that orchestrates Claude Code and Codex as fixed roles (Lead, Analyst, Designer, Developer, Tester, Verifier) over a file-based task workflow. Tasks move through stages (analysis, design, implementation, independent tests, knowledge update, done) with fingerprinted evidence gates, a parallel runner with scoped file locks, automatic waiting on rate limits, and mandatory human gates for DB changes, deployments, pushes and other high-impact actions. License: not decided yet.

## Ne yapar
- **Roller:** Lead (tek kullanıcı teması, kod yazmaz), Analyst, Designer, Developer (tek kaynak yazarı, kilitli), Tester, Verifier/Reporter (bağımsız ikinci doğrulama; Codex veya `TEAM_VERIFIER_ENGINE=claude`).
- **Aşamalar ve profiller:** `tweak`, `small`, `ui`, `full`, `risky`. Aşamalar: NEW, ANALYSIS, DESIGN, IMPLEMENTATION, TEST_CLAUDE, VERIFY_CODEX, KNOWLEDGE_UPDATE, DONE.
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

## Yeni projeye bağlama
```
cd /yol/proje
team init [yol]   # eksik .ai/PROJECT.md, models.json, runner.json oluşturur; varsa dokunmaz
```
Ardından `ONBOARDING.md` adımlarıyla `.ai/PROJECT.md`, `OWNERSHIP.md`, `CURRENT_STATE.md`, `roles/` ve `tasks/` yapısını kur. Proje yollarını `~/.config/ai-team/projects.json` içine yaz (örnek: `projects.example.json`). Kurallar projede `.ai/TEAM_RULES.md` olarak bulunabilir (projeye özel istisnalar yalnız orada); yoksa bu depodaki `TEAM_RULES.md` yedek olarak okunur.

## Temel komutlar
```
team new ID "Kısa başlık" --profile small --auto
team status [ID]            team inbox lead --task ID
team advance ID STAGE       team claim ID --session S / team release ID
team evidence ID --role tester|verifier --result PASS|FAIL|BLOCKED --session S --note "..."
team rework ID --reason "..."   team reclassify ID PROFIL --reason "..."
team knowledge ID --summary "..."   team gate ID --kind DB|HIGH_IMPACT|UNKNOWN_IMPACT ...
team automate ID --resume|--disable
team-runner --project /yol/proje watch | status
```
Tüm komutlar `--project <yol>` ile veya proje klasöründen çalışır; ayrıntı için `team --help`.

## Yapılandırma (proje içinde, hepsi isteğe bağlı)
- `.ai/models.json`: rol -> model (`developer_small` tweak/small için).
- `.ai/runner.json`: `max_parallel`, `serial_roles`; ayrıca `role_prompt_extra` (tüm rol istemlerine eklenen projeye özel kurallar), `codex_model`, `env` ve `env_by_role` (rol süreçlerine ek ortam değişkenleri).
- TASK.md `## Dosyalar`: satır başına bir glob; kanıt/parmak izi yalnız bu dosyalardan hesaplanır, kesişmeyen görevler paralel çalışır. Bölüm yoksa görev tüm repoyu kapsar.

## Güvenlik notu
Markdown kuralları ve rol etiketleri bir işletim sistemi güvenlik duvarı değildir. Standart araç izinlerini/sandbox'ı koru, bypass kullanma; üretim erişimini ayrıca dar hesap/test altyapısıyla sınırla. DB, deployment, push ve merge işlemlerini insan onaylar ve uygular.

## Testler
```
python3 bin/test_team.py && python3 bin/test_team_runner.py
```

## Lisans
Henüz belirlenmedi.
