# Proje onboarding

Ana kaynak: `skills/orchestra-setup/SKILL.md` (install.sh `~/.claude/skills/` altına kurar). Claude Code'da proje klasöründe "bu projede gerekli kurulumları yap" veya `/orchestra-setup` de.

Elle özet:
1. `team doctor` ve `team init --dry-run`, sonra `team init` (yalnız eksikleri yazar).
2. `.ai/PROJECT.md`, `OWNERSHIP.md`, `CURRENT_STATE.md` dosyalarını salt okunur inceleme sonucu, yalnız doğrulanmış bilgiyle doldur; bilinmeyene "bilinmiyor" yaz; secret kopyalama.
3. Yetki sınırlarını (DB, servisler, deploy/push, test hesapları, Verifier motoru) kullanıcıdan al, OWNERSHIP.md'ye yaz.
4. İstersen `.ai/runner.json` `role_prompt_extra` ile proje test/analiz komutlarını tanımla.
5. İlk görev: `team new ID "Başlık" --profile small --auto`. Kapanış: Tester PASS + bağımsız Verifier PASS + knowledge güncellemesi.
6. Konum/kapsam değişirse proje belgelerini güncelle; eski test kanıtları geçersizdir.
