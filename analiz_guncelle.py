"""
BIST Teknik Analiz Güncelleyici
=================================
Claude Code + TradingView kullanarak hisseler/ klasöründeki
TXT dosyalarını otomatik günceller.

Çalışma şekli:
  1. hisseler/ klasöründeki tüm .txt dosyalarını tarar
  2. Her hisse için Claude Code'a TradingView analizi yaptırır
  3. Analiz sonucunda destek/direnç seviyelerini .txt dosyasına yazar
  4. bist_sinyal_bot.py bir sonraki taramada yeni seviyeleri alır

Kurulum:
  pip install requests schedule python-dotenv
  npm install -g @anthropic-ai/claude-code   (Claude Code)

Kullanım:
  python analiz_guncelle.py                  # Tek seferlik çalıştır
  python analiz_guncelle.py --loop           # Her 2 saatte otomatik
"""

import os
import glob
import json
import argparse
import logging
import schedule
import time
import subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

HISSELER_KLASOR  = os.getenv("HISSELER_KLASOR", "hisseler")
SEANS_BASLANGIC  = 10
SEANS_BITIS      = 18

# ─────────────────────────────────────────
#  CLAUDE CODE PROMPT ŞABLONu
# ─────────────────────────────────────────

ANALIZ_PROMPT_SABLONU = """
TradingView'de BIST:{sembol} hissesini teknik analiz et.

Adımlar:
1. Chrome'da https://www.tradingview.com/chart/ adresini aç
2. Arama kutusuna "BIST:{sembol}" yaz ve grafiği aç
3. Sırasıyla şu periyotlarda analiz yap: Haftalık (1W), Günlük (1D), Saatlik (1H)
4. Her periyotta ekran görüntüsü al ve incele

Analiz et ve tam olarak aşağıdaki JSON formatında yanıt ver (başka hiçbir şey yazma):

{{
  "sembol": "{sembol}",
  "ad": "Şirket adı",
  "destek_guclu": 0.00,
  "destek_orta": 0.00,
  "direnc_1": 0.00,
  "direnc_2": 0.00,
  "direnc_3": 0.00,
  "stop_yuzde": 0.04,
  "hacim_carpani": 1.5,
  "trend": "YUKARI/ASAGI/YATAY",
  "formasyon": "Tespit edilen formasyon veya YOK",
  "ozet": "2-3 cümle teknik görünüm özeti",
  "guncelleme": "{tarih}"
}}

Kurallar:
- Tüm fiyat değerleri gerçek TradingView grafiğinden alınmalı
- destek_guclu: En güçlü destek (seans kapatmalı)
- destek_orta: İkinci destek seviyesi
- direnc_1/2/3: Sıralı direnç seviyeleri (yakından uzağa)
- stop_yuzde: Volatiliteye göre 0.03-0.06 arası
- hacim_carpani: Hacim filtresi, genelde 1.5
- Sadece JSON döndür, markdown kod bloğu kullanma
"""


# ─────────────────────────────────────────
#  CLAUDE CODE ÇALIŞTIRICI
# ─────────────────────────────────────────

def claude_code_calistir(prompt: str, timeout: int = 120) -> Optional[str]:
    """
    Claude Code'u subprocess ile çalıştırır.
    --chrome flag'i ile Chrome'a bağlanır.
    Yanıtı string olarak döndürür.
    """
    try:
        sonuc = subprocess.run(
            ["claude", "--chrome", "--print", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
        if sonuc.returncode != 0:
            log.error(f"Claude Code hata kodu {sonuc.returncode}: {sonuc.stderr[:200]}")
            return None
        return sonuc.stdout.strip()
    except FileNotFoundError:
        log.error("'claude' komutu bulunamadı. 'npm install -g @anthropic-ai/claude-code' çalıştırın.")
        return None
    except subprocess.TimeoutExpired:
        log.error(f"Claude Code zaman aşımı ({timeout}s).")
        return None
    except Exception as e:
        log.error(f"Claude Code çalıştırma hatası: {e}")
        return None


# ─────────────────────────────────────────
#  JSON AYRIŞTIRICI
# ─────────────────────────────────────────

def json_ayristir(yanit: str) -> Optional[dict]:
    """
    Claude'un yanıtından JSON'u ayıklar.
    Markdown kod bloğu varsa temizler.
    """
    if not yanit:
        return None
    temiz = yanit.strip()
    # ```json ... ``` bloğunu temizle
    if "```" in temiz:
        satirlar = temiz.split("\n")
        satirlar = [s for s in satirlar if not s.strip().startswith("```")]
        temiz = "\n".join(satirlar).strip()
    try:
        return json.loads(temiz)
    except json.JSONDecodeError:
        # JSON bloğunu bul
        bas = temiz.find("{")
        son = temiz.rfind("}") + 1
        if bas >= 0 and son > bas:
            try:
                return json.loads(temiz[bas:son])
            except json.JSONDecodeError:
                pass
        log.error(f"JSON ayrıştırılamadı: {temiz[:200]}")
        return None


# ─────────────────────────────────────────
#  TXT DOSYASI YAZICI
# ─────────────────────────────────────────

def txt_guncelle(sembol: str, veri: dict) -> bool:
    """
    Analiz sonucunu hisseler/SEMBOL.txt dosyasına yazar.
    Eski dosyanın yedeğini .bak olarak saklar.
    """
    dosya_yolu = os.path.join(HISSELER_KLASOR, f"{sembol}.txt")

    # Yedek al
    if os.path.exists(dosya_yolu):
        yedek_yolu = dosya_yolu.replace(".txt", ".bak")
        try:
            with open(dosya_yolu) as f:
                eski = f.read()
            with open(yedek_yolu, "w", encoding="utf-8") as f:
                f.write(eski)
        except Exception as e:
            log.warning(f"Yedek alınamadı: {e}")

    tarih = datetime.now().strftime("%d.%m.%Y %H:%M")
    icerik = f"""\
# {veri.get('ad', sembol)} — Teknik Seviyeler
# Son güncelleme : {tarih}
# Trend          : {veri.get('trend', '—')}
# Formasyon      : {veri.get('formasyon', '—')}
# Özet           : {veri.get('ozet', '—')}

ad             = {veri.get('ad', sembol)}
destek_guclu   = {veri.get('destek_guclu', 0):.2f}
destek_orta    = {veri.get('destek_orta', 0):.2f}
direnc_1       = {veri.get('direnc_1', 0):.2f}
direnc_2       = {veri.get('direnc_2', 0):.2f}
direnc_3       = {veri.get('direnc_3', 0):.2f}
stop_yuzde     = {veri.get('stop_yuzde', 0.04):.2f}
hacim_carpani  = {veri.get('hacim_carpani', 1.5):.1f}
"""

    try:
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(icerik)
        log.info(f"✅ {dosya_yolu} güncellendi.")
        return True
    except Exception as e:
        log.error(f"Dosya yazılamadı {dosya_yolu}: {e}")
        return False


# ─────────────────────────────────────────
#  TEK HİSSE ANALİZİ
# ─────────────────────────────────────────

def hisse_analiz_et(sembol: str) -> bool:
    """Tek bir hisse için Claude Code analizi yapar ve TXT'yi günceller."""
    log.info(f"[ANALİZ] {sembol} analiz ediliyor...")

    tarih  = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = ANALIZ_PROMPT_SABLONU.format(sembol=sembol, tarih=tarih)

    yanit = claude_code_calistir(prompt, timeout=180)
    if not yanit:
        log.error(f"[ANALİZ] {sembol}: Claude Code yanıt vermedi.")
        return False

    veri = json_ayristir(yanit)
    if not veri:
        log.error(f"[ANALİZ] {sembol}: JSON ayrıştırılamadı.")
        log.debug(f"Ham yanıt: {yanit[:500]}")
        return False

    # Temel doğrulama
    zorunlu = ["destek_guclu", "destek_orta", "direnc_1", "direnc_2", "direnc_3"]
    eksik   = [a for a in zorunlu if a not in veri or veri[a] == 0]
    if eksik:
        log.error(f"[ANALİZ] {sembol}: Eksik/sıfır alanlar: {eksik}")
        return False

    log.info(
        f"[ANALİZ] {sembol}: destek={veri['destek_guclu']}/{veri['destek_orta']} "
        f"| direnç={veri['direnc_1']}/{veri['direnc_2']}/{veri['direnc_3']} "
        f"| trend={veri.get('trend','?')}"
    )

    return txt_guncelle(sembol, veri)


# ─────────────────────────────────────────
#  TÜM HİSSELERİ ANALİZ ET
# ─────────────────────────────────────────

def tum_hisseleri_analiz_et():
    """hisseler/ klasöründeki tüm .txt dosyalarını analiz eder."""
    if not seans_uygun():
        log.info("[ANALİZ] Seans saati değil, analiz atlandı.")
        return

    dosyalar = sorted(glob.glob(os.path.join(HISSELER_KLASOR, "*.txt")))
    semboller = [
        os.path.splitext(os.path.basename(d))[0].upper()
        for d in dosyalar
        if not d.endswith(".bak")
    ]

    if not semboller:
        log.warning("[ANALİZ] Hiç hisse dosyası bulunamadı.")
        return

    log.info(f"[ANALİZ] {len(semboller)} hisse analiz edilecek: {', '.join(semboller)}")

    basarili = 0
    for sembol in semboller:
        if hisse_analiz_et(sembol):
            basarili += 1
        # Her hisse arasında kısa bekleme (TradingView'i bunaltmamak için)
        time.sleep(5)

    log.info(f"[ANALİZ] Tamamlandı: {basarili}/{len(semboller)} hisse güncellendi.")


def seans_uygun() -> bool:
    """Analiz için uygun saat mi? Seans açılmadan 30 dk önce veya seans içinde."""
    simdi = datetime.now()
    if simdi.weekday() >= 5:
        return False
    # 09:30 ile 18:00 arası analiz yap
    return 9 <= simdi.hour < SEANS_BITIS or (simdi.hour == 9 and simdi.minute >= 30)


# ─────────────────────────────────────────
#  GİRİŞ NOKTASI
# ─────────────────────────────────────────

def main():
    from typing import Optional   # geç import (subprocess kullanımı için)

    parser = argparse.ArgumentParser(description="BIST Teknik Analiz Güncelleyici")
    parser.add_argument("--loop",   action="store_true", help="Her 2 saatte otomatik çalıştır")
    parser.add_argument("--simdi",  action="store_true", help="Hemen bir kez çalıştır")
    parser.add_argument("--sembol", type=str,            help="Sadece belirli bir hisseyi analiz et")
    args = parser.parse_args()

    if not os.path.isdir(HISSELER_KLASOR):
        os.makedirs(HISSELER_KLASOR)
        log.warning(f"'{HISSELER_KLASOR}/' klasörü oluşturuldu. Önce hisse dosyalarını ekleyin.")
        return

    log.info("=" * 50)
    log.info("  BIST Teknik Analiz Güncelleyici")
    log.info(f"  Klasör  : {os.path.abspath(HISSELER_KLASOR)}/")
    log.info(f"  Mod     : {'Döngü (2 saat)' if args.loop else 'Tek seferlik'}")
    log.info("=" * 50)

    if args.sembol:
        # Tek hisse analizi
        hisse_analiz_et(args.sembol.upper())
        return

    if args.loop:
        # Döngü modu — her 2 saatte bir çalıştır
        # Ayrıca seans başında (09:30) ve öğle arası (13:00) çalıştır
        tum_hisseleri_analiz_et()   # Hemen bir kez çalıştır

        schedule.every(2).hours.do(tum_hisseleri_analiz_et)
        schedule.every().day.at("09:30").do(tum_hisseleri_analiz_et)
        schedule.every().day.at("13:00").do(tum_hisseleri_analiz_et)

        log.info("Döngü başladı. Çalışma zamanları: 09:30, 13:00 ve her 2 saatte bir.")
        log.info("Durdurmak için Ctrl+C")
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        # Tek seferlik
        tum_hisseleri_analiz_et()


if __name__ == "__main__":
    from typing import Optional
    main()
