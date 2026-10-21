---
name: orchestra-setup
description: Kullanıcı "bu projede gerekli kurulumları yap", "ai-orchestra kur", "orchestra setup" dediğinde ya da /orchestra-setup çağırdığında bu projeye ai-orchestra (team init + proje belgeleri) kurulumunu yapar.
---
# orchestra-setup

Amaç: bu projeyi ai-orchestra ile çalışır hâle getir. Kullanıcı değişikliklerini koru; DB, migration, deploy, push, merge ve gerçek veri işlemi YAPMA. Commit yapma.

1. **Ortam:** `team doctor` çalıştır. `team` yoksa kullanıcıya ai-orchestra deposunda `./install.sh` çalıştırmasını söyle ve dur.
2. **Git:** `git status --short` oku. Mevcut dirty dosyalar kullanıcınındır; dokunma, sahiplenme.
3. **İskelet:** `team init --dry-run` çıktısını kullanıcıya göster, sonra `team init`. Var olan hiçbir dosya değişmez. Çıktıda "CLAUDE.md mevcut" uyarısı varsa verilen ai-orchestra satırını elle eklemesini öner (sen değiştirme).
4. **Projeyi salt okunur incele:** manifest (package.json, pubspec.yaml, *.csproj, pyproject.toml, go.mod vb.), dil/çatı, giriş noktaları, test dizinleri, CI dosyaları. `.ai/PROJECT.md`, `.ai/OWNERSHIP.md`, `.ai/CURRENT_STATE.md` dosyalarını yalnız doğrulanmış bilgiyle doldur; bilinmeyene "bilinmiyor" yaz. Sır, token, .env/config değeri kopyalama.
5. **Runner ayarı:** proje türüne göre `.ai/runner.json` içine `role_prompt_extra` (tüm rol istemlerine eklenen metin) öner ve kullanıcı onayıyla yaz. Örnekler: Flutter: "Değişiklikten sonra `flutter analyze` ve ilgili testleri koş"; .NET: "`dotnet build` ve ilgili testler"; Node/web: "lint + ilgili testler"; Python: "ruff/pytest ilgili testler". Diğer anahtarlar: `max_parallel`, `serial_roles`, `codex_model`, `env`, `env_by_role`. Ayrıca `risk_paths` öner (DB/migration, kimlik doğrulama, ödeme, ortak çekirdek, yapılandırma globları; örn. `["**/migrations/**", "lib/core/**"]`): `fix` profiliyle açılan dar düzeltmeler bunlara dokunursa bağımsız doğrulamaya yükselir. Küçük hata düzeltmeleri için kullanıcıya `--profile fix` (ya da `--profile auto`) kullanımını anlat. İstenirse `budget` (`daily_usd`/`weekly_usd`/`task_usd`, `action`: `warn`|`pause`) ve `require_independent_verifier` sor. `team init` çıktısındaki `.gitignore` önerisini kullanıcıya göster (sen yazma).
6. **Yetki soruları (yalnız bunlar):** veritabanını kim uygular; backend/diğer servislere yazma yetkisi; deploy/push kimde; test hesapları/ortamları; Verifier için Codex kullanılsın mı (hayırsa `env: {"TEAM_VERIFIER_ENGINE": "claude"}`). Cevapları `.ai/OWNERSHIP.md` ve gerekirse `.ai/TEAM_RULES.md` (yalnız proje istisnası) içine yaz. Başka şey sorma.
7. **Bitiş:** `team doctor` çalıştır; kısa rapor ver: oluşturulan/atlanan dosyalar, doldurulan belgeler, "bilinmiyor" kalanlar, sonraki adım (`team new ID "Başlık" --profile small --auto`).
