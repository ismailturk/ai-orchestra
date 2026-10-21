# Proje onboarding
1. Klasörü ve Git kökünü bul; eski/arşiv kopyayı aktif projeyle karıştırma. Mevcut AGENTS/CLAUDE ve dirty status'u oku; kullanıcı çalışmalarını koru.
2. .ai/PROJECT.md: ürün amacı, kaynak konumu, katmanlar, doğrulanmış bilgiler ve bilinmeyenler. .ai/OWNERSHIP.md: kullanıcının yetkisi ve değiştirilemeyen alanlar.
3. Manifest, giriş noktaları, servisler, auth, UI, testler ve arka plan başlangıç yan etkilerini salt okunur incele. Secret/config değerlerini dokümana kopyalama.
4. ARCHITECTURE/BACKEND/MOBILE/DATABASE/API/AUTH/UI/CONVENTIONS belgelerine yalnız kaynaktan doğrulanan bilgileri yaz. Canlı DB bağlantısı, API çağrısı veya migration yapma.
5. KNOWN_ISSUES: gerçek bulgu, dosya referansı, etki, doğrulama durumu. DECISIONS: ownership/gates ve karar kaynakları. CURRENT_STATE: son durum, branch, blocker, sonraki adım.
6. roles/ ve tasks/ şablonları, messages/ kutuları oluştur. Backend/DB sınırını (varsa) dosyalara ve role başlangıcına koy.
7. Launcher --dry-run ve koordinasyon scriptinin izole testlerini çalıştır; proje uygulamasını çalıştırmakla karıştırma.
8. Lead ilk görevde PROJECT + CURRENT_STATE'den devam eder. Görev sonu knowledge update, Claude PASS ve bağımsız Codex PASS kapanış koşuludur.
9. Konum/kapsam değişirse project.json source_roots ve proje belgelerini birlikte güncelle; eski test kanıtlarını geçersiz say.
