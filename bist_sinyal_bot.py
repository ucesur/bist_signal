"""
BIST Hisse Senedi Sinyal Botu
==============================
Hisseler  : hisseler/ klasöründeki .txt dosyalarından otomatik yüklenir
Periyot   : 1 dakika (schedule)
Bildirim  : Telegram + Gmail (smtplib)
Veri      : Bigpara (~15 dk gecikmeli, kayıt gerekmez)
Simülasyon: 10.000 TL başlangıç bakiyesi, gün sonu e-posta raporu

Yeni hisse eklemek için:
  hisseler/SEMBOL.txt dosyası oluşturun — bot bir sonraki taramada otomatik alır.

Kurulum:
  pip install requests schedule python-dotenv
"""

import os
import glob
import requests
import schedule
import smtplib
import time
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
#  LOGLAMA
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  AYARLAR
# ─────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_GONDEREN   = os.getenv("GMAIL_GONDEREN", "")
GMAIL_SIFRE      = os.getenv("GMAIL_SIFRE", "")
GMAIL_ALICI      = os.getenv("GMAIL_ALICI", "")
TELEGRAM_AKTIF   = os.getenv("TELEGRAM_AKTIF", "true").lower() == "true"
EMAIL_AKTIF      = os.getenv("EMAIL_AKTIF", "true").lower() == "true"

HISSELER_KLASOR  = os.getenv("HISSELER_KLASOR", "hisseler")

SEANS_BASLANGIC  = 10
SEANS_BITIS      = 18
KOMISYON_ORANI   = 0.001

# ─────────────────────────────────────────
#  HİSSE YÜKLEME — TXT DOSYALARINDAN
# ─────────────────────────────────────────

ZORUNLU_ALANLAR = [
    "ad", "destek_guclu", "destek_orta",
    "direnc_1", "direnc_2", "direnc_3",
    "stop_yuzde", "hacim_carpani",
]

def hisse_yukle(dosya_yolu: str) -> Optional[tuple]:
    """
    Tek bir .txt dosyasını okur, hisse sözlüğüne dönüştürür.
    Döner: (sembol, veri_dict) veya None (hata varsa)

    Dosya formatı (SEMBOL.txt):
        ad             = Kocaer Çelik
        destek_guclu   = 11.00
        destek_orta    = 11.80
        direnc_1       = 12.20
        direnc_2       = 12.60
        direnc_3       = 14.05
        stop_yuzde     = 0.04
        hacim_carpani  = 1.5
    """
    sembol = os.path.splitext(os.path.basename(dosya_yolu))[0].upper()
    veri   = {}
    try:
        with open(dosya_yolu, encoding="utf-8") as f:
            for satir_no, satir in enumerate(f, 1):
                satir = satir.strip()
                if not satir or satir.startswith("#"):
                    continue
                if "=" not in satir:
                    log.warning(f"{dosya_yolu}:{satir_no} — '=' bulunamadı, atlandı: {satir!r}")
                    continue
                anahtar, _, deger = satir.partition("=")
                anahtar = anahtar.strip().lower()
                deger   = deger.strip()
                # Sayısal alanları float'a çevir
                if anahtar != "ad":
                    try:
                        deger = float(deger.replace(",", "."))
                    except ValueError:
                        log.warning(f"{dosya_yolu}:{satir_no} — '{anahtar}' sayıya çevrilemedi: {deger!r}")
                        return None
                veri[anahtar] = deger

        # Zorunlu alan kontrolü
        eksik = [a for a in ZORUNLU_ALANLAR if a not in veri]
        if eksik:
            log.error(f"{dosya_yolu} — Eksik alanlar: {eksik}")
            return None

        return sembol, veri

    except FileNotFoundError:
        log.error(f"{dosya_yolu} — Dosya bulunamadı.")
        return None
    except Exception as e:
        log.error(f"{dosya_yolu} — Okuma hatası: {e}")
        return None


def hisseleri_tara() -> dict:
    """
    hisseler/ klasöründeki tüm .txt dosyalarını tarar ve HISSELER sözlüğünü döndürür.
    Yeni dosya eklendiyse otomatik alır, silindiyse çıkarır.
    """
    if not os.path.isdir(HISSELER_KLASOR):
        os.makedirs(HISSELER_KLASOR)
        log.info(f"'{HISSELER_KLASOR}/' klasörü oluşturuldu.")

    dosyalar = glob.glob(os.path.join(HISSELER_KLASOR, "*.txt"))
    hisseler = {}
    for dosya in sorted(dosyalar):
        sonuc = hisse_yukle(dosya)
        if sonuc:
            sembol, veri = sonuc
            hisseler[sembol] = veri

    return hisseler


# ─────────────────────────────────────────
#  ÖRNEK TXT DOSYALARI OLUŞTUR
# ─────────────────────────────────────────

ORNEK_HISSELER = {
    "KCAER.txt": """\
# Kocaer Çelik — Teknik Seviyeler
# Güncelleme: 18.03.2026
# Satırları # ile yoruma alabilirsiniz.

ad             = Kocaer Çelik
destek_guclu   = 11.00
destek_orta    = 11.80
direnc_1       = 12.20
direnc_2       = 12.60
direnc_3       = 14.05
stop_yuzde     = 0.04
hacim_carpani  = 1.5
""",
    "ECILC.txt": """\
# Eczacıbaşı İlaç — Teknik Seviyeler
# Güncelleme: 18.03.2026

ad             = Eczacıbaşı İlaç
destek_guclu   = 112.00
destek_orta    = 114.00
direnc_1       = 117.00
direnc_2       = 120.00
direnc_3       = 128.00
stop_yuzde     = 0.03
hacim_carpani  = 1.5
""",
    "TTRAK.txt": """\
# Türk Traktör — Teknik Seviyeler
# Güncelleme: 18.03.2026

ad             = Türk Traktör
destek_guclu   = 440.00
destek_orta    = 460.00
direnc_1       = 480.00
direnc_2       = 502.50
direnc_3       = 575.00
stop_yuzde     = 0.04
hacim_carpani  = 1.5
""",
}


def ornek_dosyalari_olustur():
    """Hisseler klasörü boşsa örnek dosyaları oluşturur."""
    if not os.path.isdir(HISSELER_KLASOR):
        os.makedirs(HISSELER_KLASOR)
    mevcut = glob.glob(os.path.join(HISSELER_KLASOR, "*.txt"))
    if mevcut:
        return  # Zaten dosya var, dokunma
    for dosya_adi, icerik in ORNEK_HISSELER.items():
        yol = os.path.join(HISSELER_KLASOR, dosya_adi)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(icerik)
        log.info(f"Örnek dosya oluşturuldu: {yol}")


# ─────────────────────────────────────────
#  SİMÜLASYON — PORTFÖY DURUMU
# ─────────────────────────────────────────

@dataclass
class Pozisyon:
    sembol:      str
    ad:          str
    adet:        int
    alis_fiyati: float
    alis_zamani: str

@dataclass
class Islem:
    zaman:     str
    sembol:    str
    tip:       str
    fiyat:     float
    adet:      int
    tutar:     float
    komisyon:  float
    kar_zarar: Optional[float]
    neden:     str

@dataclass
class Portfolyo:
    baslangic_bakiye:         float = 10_000.0
    nakit:                    float = 10_000.0
    pozisyonlar:              dict  = field(default_factory=dict)
    islemler:                 list  = field(default_factory=list)
    gun_baslangic:            str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    POZISYON_BUYUKLUK_YUZDESI: float = 0.30

    def toplam_deger(self, guncel_fiyatlar: dict) -> float:
        hisse_degeri = sum(
            p.adet * guncel_fiyatlar.get(p.sembol, p.alis_fiyati)
            for p in self.pozisyonlar.values()
        )
        return self.nakit + hisse_degeri

    def kar_zarar_toplam(self, guncel_fiyatlar: dict) -> float:
        return self.toplam_deger(guncel_fiyatlar) - self.baslangic_bakiye

    def kar_zarar_yuzde(self, guncel_fiyatlar: dict) -> float:
        return (self.kar_zarar_toplam(guncel_fiyatlar) / self.baslangic_bakiye) * 100


portfolyo        = Portfolyo()
_guncel_fiyatlar: dict = {}


def portfolyo_al(sembol: str, fiyat: float, neden: str, hisseler: dict) -> bool:
    if sembol in portfolyo.pozisyonlar:
        log.info(f"[SİMÜLASYON] {sembol}: Zaten pozisyon var, atlandı.")
        return False
    ayrilacak = portfolyo.nakit * portfolyo.POZISYON_BUYUKLUK_YUZDESI
    if ayrilacak < fiyat:
        log.info(f"[SİMÜLASYON] {sembol}: Nakit yetersiz ({portfolyo.nakit:.2f} TL).")
        return False
    adet     = int(ayrilacak / fiyat)
    if adet == 0:
        return False
    tutar    = adet * fiyat
    komisyon = tutar * KOMISYON_ORANI
    portfolyo.nakit -= (tutar + komisyon)
    portfolyo.pozisyonlar[sembol] = Pozisyon(
        sembol=sembol, ad=hisseler[sembol]["ad"],
        adet=adet, alis_fiyati=fiyat,
        alis_zamani=datetime.now().strftime("%H:%M"),
    )
    portfolyo.islemler.append(Islem(
        zaman=datetime.now().strftime("%H:%M"), sembol=sembol,
        tip="ALIM", fiyat=fiyat, adet=adet, tutar=tutar,
        komisyon=komisyon, kar_zarar=None, neden=neden,
    ))
    log.info(f"[SİMÜLASYON] ALIM | {sembol} | {adet} adet @ {fiyat} TL | Nakit: {portfolyo.nakit:.2f} TL")
    return True


def portfolyo_sat(sembol: str, fiyat: float, neden: str) -> bool:
    if sembol not in portfolyo.pozisyonlar:
        return False
    poz      = portfolyo.pozisyonlar[sembol]
    tutar    = poz.adet * fiyat
    komisyon = tutar * KOMISYON_ORANI
    net      = tutar - komisyon
    kar_zarar = net - poz.adet * poz.alis_fiyati - poz.adet * poz.alis_fiyati * KOMISYON_ORANI
    portfolyo.nakit += net
    del portfolyo.pozisyonlar[sembol]
    portfolyo.islemler.append(Islem(
        zaman=datetime.now().strftime("%H:%M"), sembol=sembol,
        tip="SATIM", fiyat=fiyat, adet=poz.adet, tutar=tutar,
        komisyon=komisyon, kar_zarar=kar_zarar, neden=neden,
    ))
    kz_str = f"+{kar_zarar:.2f}" if kar_zarar >= 0 else f"{kar_zarar:.2f}"
    log.info(f"[SİMÜLASYON] SATIM | {sembol} | {poz.adet} adet @ {fiyat} TL | K/Z: {kz_str} TL")
    return True


def stop_loss_kontrol(sembol: str, fiyat: float, hisseler: dict):
    if sembol not in portfolyo.pozisyonlar:
        return
    poz        = portfolyo.pozisyonlar[sembol]
    stop_fiyat = round(poz.alis_fiyati * (1 - hisseler[sembol]["stop_yuzde"]), 2)
    if fiyat <= stop_fiyat:
        log.warning(f"[SİMÜLASYON] STOP-LOSS! {sembol} @ {fiyat} TL (stop: {stop_fiyat} TL)")
        portfolyo_sat(sembol, fiyat, f"Stop-loss ({stop_fiyat} TL)")


def updateBalance(sinyal: "Sinyal", veri: dict, hisseler: dict):
    fiyat  = veri["fiyat"]
    sembol = sinyal.sembol
    _guncel_fiyatlar[sembol] = fiyat
    stop_loss_kontrol(sembol, fiyat, hisseler)
    if sinyal.tip == "ALIM" and sinyal.guc in ("GÜÇLÜ", "NORMAL", "KIRILIM"):
        portfolyo_al(sembol, fiyat, sinyal.neden, hisseler)
    elif sinyal.tip == "SATIS":
        portfolyo_sat(sembol, fiyat, sinyal.neden)


# ─────────────────────────────────────────
#  VERİ ÇEKME — Bigpara API
# ─────────────────────────────────────────

_hacim_gecmisi: dict = {}
HACIM_PENCERE  = 20

BIGPARA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://bigpara.hurriyet.com.tr/",
}


def fiyat_cek(sembol: str, deneme: int = 3) -> Optional[dict]:
    url = f"https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{sembol}"
    for i in range(1, deneme + 1):
        try:
            r = requests.get(url, headers=BIGPARA_HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {}).get("hisseYuzeysel", {})
            if not data:
                log.warning(f"{sembol}: Bigpara boş veri döndürdü.")
                return None
            alis    = data.get("alis")
            kapanis = data.get("kapanis")
            if seans_acik():
                ham_fiyat = alis if alis is not None else kapanis
            else:
                ham_fiyat = kapanis if kapanis is not None else alis
            if ham_fiyat is None:
                log.warning(f"{sembol}: Fiyat alanı boş.")
                return None
            fiyat   = float(str(ham_fiyat).replace(",", "."))
            hacim   = _hacim_parse(data.get("hacimtl") or "0")
            degisim = float(str(data.get("yuzdedegisim") or "0").replace(",", ".").replace("%", ""))
            _guncel_fiyatlar[sembol] = fiyat
            return {
                "sembol":        sembol,
                "fiyat":         fiyat,
                "hacim":         hacim,
                "hacim_ort":     _hacim_ort_guncelle(sembol, hacim),
                "degisim_yuzde": degisim,
                "zaman":         datetime.now().strftime("%H:%M"),
            }
        except requests.exceptions.RequestException as e:
            log.warning(f"{sembol} ağ hatası (deneme {i}/{deneme}): {e}")
            if i < deneme:
                time.sleep(3 * i)
            else:
                log.error(f"{sembol}: {deneme} denemede de veri alınamadı.")
                return None
        except (KeyError, ValueError, TypeError) as e:
            log.error(f"{sembol} veri hatası: {e}")
            return None


def _hacim_parse(hacim_str: str) -> int:
    try:
        return int(float(str(hacim_str).replace(".", "").replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def _hacim_ort_guncelle(sembol: str, yeni_hacim: int) -> int:
    if sembol not in _hacim_gecmisi:
        _hacim_gecmisi[sembol] = []
    gecmis = _hacim_gecmisi[sembol]
    if yeni_hacim > 0:
        gecmis.append(yeni_hacim)
    if len(gecmis) > HACIM_PENCERE:
        gecmis.pop(0)
    return int(sum(gecmis) / len(gecmis)) if gecmis else yeni_hacim or 1


# ─────────────────────────────────────────
#  SİNYAL MOTORU
# ─────────────────────────────────────────

@dataclass
class Sinyal:
    sembol:   str
    ad:       str
    tip:      str
    guc:      str
    fiyat:    float
    neden:    str
    stop:     Optional[float]
    hedef_1:  Optional[float]
    hedef_2:  Optional[float]
    hedef_3:  Optional[float]
    hacim_ok: bool
    zaman:    str


def sinyal_uret(sembol: str, veri: dict, hisseler: dict) -> Sinyal:
    s        = hisseler[sembol]
    fiyat    = veri["fiyat"]
    hacim_ok = veri["hacim"] > veri["hacim_ort"] * s["hacim_carpani"]
    stop     = round(fiyat * (1 - s["stop_yuzde"]), 2)

    if fiyat <= s["destek_guclu"] and hacim_ok:
        return Sinyal(sembol, s["ad"], "ALIM", "GÜÇLÜ", fiyat,
                      f"Güçlü destek ({s['destek_guclu']} TL) + yüksek hacim",
                      stop, s["direnc_1"], s["direnc_2"], s["direnc_3"], hacim_ok, veri["zaman"])
    elif fiyat <= s["destek_orta"] and hacim_ok:
        return Sinyal(sembol, s["ad"], "ALIM", "NORMAL", fiyat,
                      f"Destek bölgesi ({s['destek_orta']} TL) + hacim desteği",
                      stop, s["direnc_1"], s["direnc_2"], None, hacim_ok, veri["zaman"])
    elif fiyat > s["direnc_1"] and hacim_ok:
        return Sinyal(sembol, s["ad"], "ALIM", "KIRILIM", fiyat,
                      f"Direnç kırıldı ({s['direnc_1']} TL) + hacim teyidi ✅",
                      s["destek_orta"], s["direnc_2"], s["direnc_3"], None, hacim_ok, veri["zaman"])
    elif fiyat >= s["direnc_3"]:
        return Sinyal(sembol, s["ad"], "SATIS", "KAR AL", fiyat,
                      f"3. hedef ({s['direnc_3']} TL) — tüm pozisyon kapat",
                      None, None, None, None, hacim_ok, veri["zaman"])
    elif fiyat >= s["direnc_2"]:
        return Sinyal(sembol, s["ad"], "SATIS", "KAR AL", fiyat,
                      f"2. hedef ({s['direnc_2']} TL) — %50 pozisyon kapat",
                      None, None, None, None, hacim_ok, veri["zaman"])
    else:
        return Sinyal(sembol, s["ad"], "BEKLE", "NÖTR", fiyat,
                      f"Bant içi ({s['destek_orta']}–{s['direnc_1']} TL)",
                      None, None, None, None, hacim_ok, veri["zaman"])


# ─────────────────────────────────────────
#  BİLDİRİM
# ─────────────────────────────────────────

def _emoji(tip: str, guc: str) -> str:
    if tip == "ALIM" and guc == "GÜÇLÜ":   return "🟢🔥"
    if tip == "ALIM" and guc == "NORMAL":  return "🟢"
    if tip == "ALIM" and guc == "KIRILIM": return "🚀"
    if tip == "SATIS":                     return "🔴"
    return "⏳"


def _metin_olustur(s: Sinyal, html: bool = False) -> str:
    emoji     = _emoji(s.tip, s.guc)
    hacim_str = "✅ Yüksek hacim" if s.hacim_ok else "⚠️ Düşük hacim"
    cizgi     = "─" * 30
    satirlar  = [
        f"{emoji} {s.sembol} — {s.ad}", cizgi,
        f"Fiyat    : {s.fiyat} TL",
        f"Sinyal   : {s.tip} ({s.guc})",
        f"Hacim    : {hacim_str}",
        f"Neden    : {s.neden}",
    ]
    if s.stop:    satirlar.append(f"Stop-Loss: {s.stop} TL")
    if s.hedef_1: satirlar.append(f"Hedef 1  : {s.hedef_1} TL")
    if s.hedef_2: satirlar.append(f"Hedef 2  : {s.hedef_2} TL")
    if s.hedef_3: satirlar.append(f"Hedef 3  : {s.hedef_3} TL")
    satirlar += [cizgi, f"Saat: {s.zaman}", "⚠️ Bu mesaj yatırım tavsiyesi değildir."]
    if html:
        govde = "".join(f"<p>{satir}</p>" for satir in satirlar)
        return f'<html><body style="font-family:monospace;font-size:14px;">{govde}</body></html>'
    return f"{emoji} *{s.sembol} — {s.ad}*\n" + "\n".join(satirlar[1:])


def telegram_gonder(s: Sinyal) -> bool:
    if not TELEGRAM_AKTIF or not TELEGRAM_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": _metin_olustur(s), "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code == 200:
            log.info(f"Telegram gönderildi: {s.sembol}")
            return True
        log.error(f"Telegram hatası {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log.error(f"Telegram bağlantı hatası: {e}")
        return False


def send_email(s: Sinyal) -> bool:
    if not EMAIL_AKTIF or not GMAIL_GONDEREN:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[BIST] {_emoji(s.tip, s.guc)} {s.sembol} — {s.tip} ({s.guc}) @ {s.fiyat} TL"
        msg["From"]    = f"BIST Sinyal Botu <{GMAIL_GONDEREN}>"
        msg["To"]      = GMAIL_ALICI
        msg.attach(MIMEText(_metin_olustur(s, html=False), "plain", "utf-8"))
        msg.attach(MIMEText(_metin_olustur(s, html=True),  "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_GONDEREN, GMAIL_SIFRE)
            smtp.sendmail(GMAIL_GONDEREN, GMAIL_ALICI, msg.as_string())
        log.info(f"E-posta gönderildi: {s.sembol}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail kimlik doğrulama hatası! Uygulama Şifresi kullanın.")
        return False
    except Exception as e:
        log.error(f"E-posta gönderilemedi: {e}")
        return False


def bildirim_gonder(s: Sinyal):
    if TELEGRAM_AKTIF:
        telegram_gonder(s)
    if EMAIL_AKTIF:
        send_email(s)


# ─────────────────────────────────────────
#  GÜN SONU RAPORU
# ─────────────────────────────────────────

def _gun_sonu_raporu_olustur(html: bool = False) -> str:
    cizgi        = "─" * 36
    simdi        = datetime.now().strftime("%d.%m.%Y")
    toplam       = portfolyo.toplam_deger(_guncel_fiyatlar)
    kz           = portfolyo.kar_zarar_toplam(_guncel_fiyatlar)
    kz_yuzde     = portfolyo.kar_zarar_yuzde(_guncel_fiyatlar)
    kz_emoji     = "📈" if kz >= 0 else "📉"
    kz_isaretli  = f"+{kz:.2f}" if kz >= 0 else f"{kz:.2f}"
    alimlar      = [i for i in portfolyo.islemler if i.tip == "ALIM"]
    satimlar     = [i for i in portfolyo.islemler if i.tip == "SATIM"]
    komisyon_top = sum(i.komisyon for i in portfolyo.islemler)
    gercek_kz    = sum(i.kar_zarar for i in satimlar if i.kar_zarar is not None)

    satirlar = [
        f"📊 BIST SİMÜLASYON RAPORU — {simdi}", cizgi,
        f"💰 Başlangıç : {portfolyo.baslangic_bakiye:>10.2f} TL",
        f"💼 Toplam    : {toplam:>10.2f} TL",
        f"{kz_emoji} K/Z       : {kz_isaretli:>10} TL  ({kz_yuzde:+.2f}%)",
        cizgi, f"🏦 Nakit     : {portfolyo.nakit:>10.2f} TL",
    ]
    if portfolyo.pozisyonlar:
        satirlar.append("📌 Açık Pozisyonlar:")
        for sembol, poz in portfolyo.pozisyonlar.items():
            guncel = _guncel_fiyatlar.get(sembol, poz.alis_fiyati)
            poz_kz = (guncel - poz.alis_fiyati) * poz.adet
            poz_kz_str = f"+{poz_kz:.2f}" if poz_kz >= 0 else f"{poz_kz:.2f}"
            satirlar.append(f"  {sembol}: {poz.adet} adet | Alış: {poz.alis_fiyati} TL | Güncel: {guncel} TL | K/Z: {poz_kz_str} TL")
    else:
        satirlar.append("📌 Açık Pozisyon: Yok")
    satirlar += [
        cizgi, "📋 İşlem Özeti:",
        f"  Alım  : {len(alimlar)} işlem",
        f"  Satım : {len(satimlar)} işlem",
        f"  Gerçekleşen K/Z : {gercek_kz:+.2f} TL",
        f"  Toplam Komisyon  : {komisyon_top:.2f} TL",
    ]
    if portfolyo.islemler:
        satirlar.append(cizgi)
        satirlar.append("📝 İşlem Detayları:")
        for i in portfolyo.islemler:
            kz_str = f" | K/Z: {i.kar_zarar:+.2f} TL" if i.kar_zarar is not None else ""
            satirlar.append(f"  {i.zaman} | {i.tip:5s} | {i.sembol} | {i.adet} adet @ {i.fiyat} TL{kz_str}")
    satirlar += [cizgi, "⚠️ Bu rapor simülasyon verisidir, gerçek işlem değildir."]

    if html:
        govde = "".join(
            f"<p style='color:{'green' if '+' in s else ('red' if s.strip().startswith('-') else 'inherit')}'>{s}</p>"
            for s in satirlar
        )
        return f'<html><body style="font-family:monospace;font-size:13px;background:#f9f9f9;padding:20px;">{govde}</body></html>'
    return "\n".join(satirlar)


def gun_sonu_email_gonder():
    if not EMAIL_AKTIF or not GMAIL_GONDEREN:
        return
    toplam = portfolyo.toplam_deger(_guncel_fiyatlar)
    kz     = portfolyo.kar_zarar_toplam(_guncel_fiyatlar)
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{'📈' if kz>=0 else '📉'} BIST Simülasyon Raporu — {datetime.now().strftime('%d.%m.%Y')} | Toplam: {toplam:.2f} TL ({kz:+.2f} TL)"
        msg["From"]    = f"BIST Sinyal Botu <{GMAIL_GONDEREN}>"
        msg["To"]      = GMAIL_ALICI
        msg.attach(MIMEText(_gun_sonu_raporu_olustur(html=False), "plain", "utf-8"))
        msg.attach(MIMEText(_gun_sonu_raporu_olustur(html=True),  "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_GONDEREN, GMAIL_SIFRE)
            smtp.sendmail(GMAIL_GONDEREN, GMAIL_ALICI, msg.as_string())
        log.info(f"[GÜN SONU] E-posta gönderildi → {GMAIL_ALICI}")
    except Exception as e:
        log.error(f"[GÜN SONU] E-posta gönderilemedi: {e}")


def gun_sonu_telegram_gonder():
    if not TELEGRAM_AKTIF or not TELEGRAM_TOKEN:
        return
    try:
        metin = _gun_sonu_raporu_olustur(html=False)
        if len(metin) > 4000:
            metin = metin[:4000] + "\n...(tam rapor e-posta ile gönderildi)"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": metin}, timeout=10,
        )
        log.info("[GÜN SONU] Rapor Telegram'a gönderildi.")
    except Exception as e:
        log.error(f"[GÜN SONU] Telegram raporu gönderilemedi: {e}")


def gun_sonu(hisseler: dict):
    log.info("[GÜN SONU] Seans kapandı, rapor hazırlanıyor...")
    log.info("\n" + _gun_sonu_raporu_olustur())
    gun_sonu_email_gonder()
    gun_sonu_telegram_gonder()
    portfolyo.gun_baslangic = datetime.now().strftime("%Y-%m-%d")
    portfolyo.islemler.clear()
    log.info("[GÜN SONU] Portföy geçmişi temizlendi.")


# ─────────────────────────────────────────
#  SEANS KONTROLÜ
# ─────────────────────────────────────────

def seans_acik() -> bool:
    simdi = datetime.now()
    if simdi.weekday() >= 5:
        return False
    return SEANS_BASLANGIC <= simdi.hour < SEANS_BITIS


son_sinyaller: dict = {}
_seans_acikti: bool = False
_onceki_hisse_listesi: set = set()


def sinyal_degisti_mi(sembol: str, yeni: Sinyal) -> bool:
    eski = son_sinyaller.get(sembol)
    if eski is None:
        return True
    return eski.tip != yeni.tip or eski.guc != yeni.guc


# ─────────────────────────────────────────
#  ANA DÖNGÜ
# ─────────────────────────────────────────

def kontrol_et():
    global _seans_acikti, _onceki_hisse_listesi

    # ── Hisseleri her taramada yeniden yükle (yeni dosya varsa alır) ──
    hisseler = hisseleri_tara()

    if not hisseler:
        log.warning("Hiç hisse yüklenemedi! hisseler/ klasörünü kontrol edin.")
        return

    # Yeni hisse eklenip eklenmediğini bildir
    guncel_liste = set(hisseler.keys())
    eklenen      = guncel_liste - _onceki_hisse_listesi
    silinen      = _onceki_hisse_listesi - guncel_liste
    if eklenen:
        log.info(f"✅ Yeni hisse(ler) eklendi: {', '.join(sorted(eklenen))}")
    if silinen:
        log.info(f"🗑️  Hisse(ler) kaldırıldı: {', '.join(sorted(silinen))}")
        # Silinen hisselerin sinyal geçmişini temizle
        for s in silinen:
            son_sinyaller.pop(s, None)
    _onceki_hisse_listesi = guncel_liste

    acik = seans_acik()

    # Seans kapandıysa gün sonu raporu
    if _seans_acikti and not acik:
        gun_sonu(hisseler)

    _seans_acikti = acik

    if not acik:
        log.info("Seans kapalı, bekleniyor...")
        return

    log.info(f"Tarama başladı... ({len(hisseler)} hisse: {', '.join(sorted(hisseler))})")

    for sembol in sorted(hisseler):
        veri = fiyat_cek(sembol)
        if not veri:
            log.warning(f"{sembol}: veri alınamadı, atlanıyor.")
            continue

        sinyal = sinyal_uret(sembol, veri, hisseler)
        log.info(f"{sembol}: {sinyal.fiyat} TL → {sinyal.tip} ({sinyal.guc})")

        stop_loss_kontrol(sembol, sinyal.fiyat, hisseler)

        if sinyal.tip != "BEKLE" and sinyal_degisti_mi(sembol, sinyal):
            bildirim_gonder(sinyal)
            updateBalance(sinyal, veri, hisseler)

        son_sinyaller[sembol] = sinyal

    log.info("Tarama tamamlandı.")


# ─────────────────────────────────────────
#  GİRİŞ NOKTASI
# ─────────────────────────────────────────

if __name__ == "__main__":
    ornek_dosyalari_olustur()

    hisseler = hisseleri_tara()

    log.info("=" * 50)
    log.info("  BIST Sinyal Botu v1.5 — Dinamik Hisse")
    log.info(f"  Yüklenen hisseler: {', '.join(sorted(hisseler)) if hisseler else 'YOK'}")
    log.info(f"  Klasör          : {os.path.abspath(HISSELER_KLASOR)}/")
    log.info(f"  Başlangıç Bakiye: {portfolyo.baslangic_bakiye:,.0f} TL")
    log.info(f"  Telegram : {'AÇIK' if TELEGRAM_AKTIF else 'KAPALI'}")
    log.info(f"  E-posta  : {'AÇIK' if EMAIL_AKTIF else 'KAPALI'}")
    log.info("=" * 50)
    log.info("💡 Yeni hisse eklemek için: hisseler/SEMBOL.txt oluşturun")

    kontrol_et()
    schedule.every(1).minutes.do(kontrol_et)

    log.info("Bot çalışıyor. Durdurmak için Ctrl+C")
    while True:
        schedule.run_pending()
        time.sleep(30)
