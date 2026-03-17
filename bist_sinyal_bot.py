"""
BIST Hisse Senedi Sinyal Botu
==============================
Hisseler  : KCAER, ECILC, TTRAK
Periyot   : 10 dakika
Bildirim  : Telegram + Gmail (smtplib)
Veri      : Bigpara (~15 dk gecikmeli, kayıt gerekmez)
Simülasyon: 10.000 TL başlangıç bakiyesi, gün sonu e-posta raporu

Kurulum:
  pip install requests schedule python-dotenv
"""

import os
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

# ─────────────────────────────────────────
#  HİSSE TEKNİK SEVİYELERİ
# ─────────────────────────────────────────
HISSELER = {
    "KCAER": {
        "ad": "Kocaer Çelik",
        "destek_guclu":  11.50,
        "destek_orta":   11.80,
        "direnc_1":      12.20,
        "direnc_2":      12.60,
        "direnc_3":      13.00,
        "stop_yuzde":    0.04,
        "hacim_carpani": 1.5,
    },
    "ECILC": {
        "ad": "Eczacıbaşı İlaç",
        "destek_guclu":  112.00,
        "destek_orta":   114.00,
        "direnc_1":      117.00,
        "direnc_2":      120.00,
        "direnc_3":      125.00,
        "stop_yuzde":    0.03,
        "hacim_carpani": 1.5,
    },
    "TTRAK": {
        "ad": "Türk Traktör",
        "destek_guclu":  450.00,
        "destek_orta":   460.00,
        "direnc_1":      480.00,
        "direnc_2":      510.00,
        "direnc_3":      575.00,
        "stop_yuzde":    0.04,
        "hacim_carpani": 1.5,
    },
}

SEANS_BASLANGIC = 10
SEANS_BITIS     = 18
KOMISYON_ORANI  = 0.001   # %0.1 alım + %0.1 satım (standart aracı kurum)

# ─────────────────────────────────────────
#  SİMÜLASYON — PORTFÖY DURUMU
# ─────────────────────────────────────────

@dataclass
class Pozisyon:
    sembol:       str
    ad:           str
    adet:         int
    alis_fiyati:  float
    alis_zamani:  str

@dataclass
class Islem:
    zaman:    str
    sembol:   str
    tip:      str        # ALIM / SATIM
    fiyat:    float
    adet:     int
    tutar:    float
    komisyon: float
    kar_zarar: Optional[float]   # Sadece satışta dolu
    neden:    str

@dataclass
class Portfolyo:
    baslangic_bakiye: float = 10_000.0
    nakit:            float = 10_000.0
    pozisyonlar:      dict  = field(default_factory=dict)   # sembol → Pozisyon
    islemler:         list  = field(default_factory=list)   # Islem listesi
    gun_baslangic:    str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    # Her alımda portföyün kaç %'ini kullan
    POZISYON_BUYUKLUK_YUZDESI = 0.30   # %30

    def toplam_deger(self, guncel_fiyatlar: dict) -> float:
        """Nakit + açık pozisyonların anlık değeri."""
        hisse_degeri = sum(
            p.adet * guncel_fiyatlar.get(p.sembol, p.alis_fiyati)
            for p in self.pozisyonlar.values()
        )
        return self.nakit + hisse_degeri

    def kar_zarar_toplam(self, guncel_fiyatlar: dict) -> float:
        return self.toplam_deger(guncel_fiyatlar) - self.baslangic_bakiye

    def kar_zarar_yuzde(self, guncel_fiyatlar: dict) -> float:
        kz = self.kar_zarar_toplam(guncel_fiyatlar)
        return (kz / self.baslangic_bakiye) * 100

# Global portföy nesnesi
portfolyo = Portfolyo()

# Güncel fiyat hafızası (gün sonu raporu için)
_guncel_fiyatlar: dict = {}


def portfolyo_al(sembol: str, fiyat: float, neden: str) -> bool:
    """
    Sinyal geldiğinde portföyün %30'u kadar alım yapar.
    Zaten pozisyon varsa veya nakit yetersizse atlar.
    """
    if sembol in portfolyo.pozisyonlar:
        log.info(f"[SİMÜLASYON] {sembol}: Zaten pozisyon var, alım atlandı.")
        return False

    ayrilacak_nakit = portfolyo.nakit * portfolyo.POZISYON_BUYUKLUK_YUZDESI
    if ayrilacak_nakit < fiyat:
        log.info(f"[SİMÜLASYON] {sembol}: Nakit yetersiz ({portfolyo.nakit:.2f} TL).")
        return False

    adet     = int(ayrilacak_nakit / fiyat)
    if adet == 0:
        return False

    tutar    = adet * fiyat
    komisyon = tutar * KOMISYON_ORANI
    toplam   = tutar + komisyon

    portfolyo.nakit -= toplam
    portfolyo.pozisyonlar[sembol] = Pozisyon(
        sembol=sembol,
        ad=HISSELER[sembol]["ad"],
        adet=adet,
        alis_fiyati=fiyat,
        alis_zamani=datetime.now().strftime("%H:%M"),
    )
    portfolyo.islemler.append(Islem(
        zaman=datetime.now().strftime("%H:%M"),
        sembol=sembol,
        tip="ALIM",
        fiyat=fiyat,
        adet=adet,
        tutar=tutar,
        komisyon=komisyon,
        kar_zarar=None,
        neden=neden,
    ))

    log.info(
        f"[SİMÜLASYON] ALIM | {sembol} | {adet} adet @ {fiyat} TL | "
        f"Tutar: {tutar:.2f} TL | Komisyon: {komisyon:.2f} TL | "
        f"Kalan nakit: {portfolyo.nakit:.2f} TL"
    )
    return True


def portfolyo_sat(sembol: str, fiyat: float, neden: str) -> bool:
    """
    Satış sinyali veya stop-loss tetiklenince tüm pozisyonu satar.
    """
    if sembol not in portfolyo.pozisyonlar:
        log.info(f"[SİMÜLASYON] {sembol}: Pozisyon yok, satış atlandı.")
        return False

    poz      = portfolyo.pozisyonlar[sembol]
    tutar    = poz.adet * fiyat
    komisyon = tutar * KOMISYON_ORANI
    net      = tutar - komisyon

    alis_maliyeti = poz.adet * poz.alis_fiyati
    kar_zarar     = net - alis_maliyeti - (alis_maliyeti * KOMISYON_ORANI)

    portfolyo.nakit += net
    del portfolyo.pozisyonlar[sembol]

    portfolyo.islemler.append(Islem(
        zaman=datetime.now().strftime("%H:%M"),
        sembol=sembol,
        tip="SATIM",
        fiyat=fiyat,
        adet=poz.adet,
        tutar=tutar,
        komisyon=komisyon,
        kar_zarar=kar_zarar,
        neden=neden,
    ))

    kz_str = f"+{kar_zarar:.2f}" if kar_zarar >= 0 else f"{kar_zarar:.2f}"
    log.info(
        f"[SİMÜLASYON] SATIM | {sembol} | {poz.adet} adet @ {fiyat} TL | "
        f"K/Z: {kz_str} TL | Kalan nakit: {portfolyo.nakit:.2f} TL"
    )
    return True


def stop_loss_kontrol(sembol: str, fiyat: float):
    """Açık pozisyonlarda stop-loss tetiklenip tetiklenmediğini kontrol eder."""
    if sembol not in portfolyo.pozisyonlar:
        return
    poz        = portfolyo.pozisyonlar[sembol]
    stop_fiyat = round(poz.alis_fiyati * (1 - HISSELER[sembol]["stop_yuzde"]), 2)
    if fiyat <= stop_fiyat:
        log.warning(f"[SİMÜLASYON] STOP-LOSS tetiklendi! {sembol} @ {fiyat} TL (stop: {stop_fiyat} TL)")
        portfolyo_sat(sembol, fiyat, f"Stop-loss tetiklendi ({stop_fiyat} TL)")


def updateBalance(sinyal: "Sinyal", veri: dict):
    """
    Sinyal tipine göre portföyü günceller.
    ALIM sinyallerinde satın al, SATIS sinyallerinde sat.
    Her çağrıda stop-loss da kontrol edilir.
    """
    fiyat  = veri["fiyat"]
    sembol = sinyal.sembol

    # Güncel fiyatı hafızaya al (gün sonu raporu için)
    _guncel_fiyatlar[sembol] = fiyat

    # Önce stop-loss kontrolü
    stop_loss_kontrol(sembol, fiyat)

    if sinyal.tip == "ALIM" and sinyal.guc in ("GÜÇLÜ", "NORMAL", "KIRILIM"):
        portfolyo_al(sembol, fiyat, sinyal.neden)

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
    """
    Bigpara'dan hisse verisini çeker.
    Bağlantı kesilirse 3 kez yeniden dener (3s, 6s arayla).
    Seans içinde 'alis', seans dışında 'kapanis' kullanır.
    """
    url = f"https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{sembol}"
    for i in range(1, deneme + 1):
        try:
            r = requests.get(url, headers=BIGPARA_HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {}).get("hisseYuzeysel", {})
            if not data:
                log.warning(f"{sembol}: Bigpara boş veri döndürdü.")
                return None

            # or operatörü 0.0'ı falsy sayar — None kontrolü ile yapıyoruz
            alis    = data.get("alis")
            kapanis = data.get("kapanis")

            if seans_acik():
                ham_fiyat = alis if alis is not None else kapanis
                kaynak    = "alis"
            else:
                ham_fiyat = kapanis if kapanis is not None else alis
                kaynak    = "kapanis"

            if ham_fiyat is None:
                log.warning(f"{sembol}: Fiyat alanı boş.")
                return None

            fiyat   = float(str(ham_fiyat).replace(",", "."))
            hacim   = _hacim_parse(data.get("hacimtl") or "0")
            degisim = float(str(data.get("yuzdedegisim") or "0").replace(",", ".").replace("%", ""))

            log.debug(f"{sembol}: {fiyat} TL ({kaynak}), hacim={hacim:,}")
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
                time.sleep(3 * i)   # 1. retry: 3s, 2. retry: 6s
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


def sinyal_uret(sembol: str, veri: dict) -> Sinyal:
    s        = HISSELER[sembol]
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
#  BİLDİRİM — ORTAK METİN ÜRETİCİ
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

    satirlar = [
        f"{emoji} {s.sembol} — {s.ad}",
        cizgi,
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


# ─────────────────────────────────────────
#  GÜN SONU RAPORU
# ─────────────────────────────────────────

def _gun_sonu_raporu_olustur(html: bool = False) -> str:
    """Portföy durumunu özetleyen gün sonu raporu üretir."""
    cizgi      = "─" * 36
    simdi      = datetime.now().strftime("%d.%m.%Y")
    toplam     = portfolyo.toplam_deger(_guncel_fiyatlar)
    kz         = portfolyo.kar_zarar_toplam(_guncel_fiyatlar)
    kz_yuzde   = portfolyo.kar_zarar_yuzde(_guncel_fiyatlar)
    kz_emoji   = "📈" if kz >= 0 else "📉"
    kz_isaretli = f"+{kz:.2f}" if kz >= 0 else f"{kz:.2f}"

    # İşlem özeti
    alimlar  = [i for i in portfolyo.islemler if i.tip == "ALIM"]
    satimlar = [i for i in portfolyo.islemler if i.tip == "SATIM"]
    toplam_komisyon = sum(i.komisyon for i in portfolyo.islemler)
    gerceklesen_kz  = sum(i.kar_zarar for i in satimlar if i.kar_zarar is not None)

    satirlar = [
        f"📊 BIST SİMÜLASYON RAPORU — {simdi}",
        cizgi,
        f"💰 Başlangıç : {portfolyo.baslangic_bakiye:>10.2f} TL",
        f"💼 Toplam    : {toplam:>10.2f} TL",
        f"{kz_emoji} K/Z       : {kz_isaretli:>10} TL  ({kz_yuzde:+.2f}%)",
        cizgi,
        f"🏦 Nakit     : {portfolyo.nakit:>10.2f} TL",
    ]

    # Açık pozisyonlar
    if portfolyo.pozisyonlar:
        satirlar.append(f"📌 Açık Pozisyonlar:")
        for sembol, poz in portfolyo.pozisyonlar.items():
            guncel = _guncel_fiyatlar.get(sembol, poz.alis_fiyati)
            poz_kz = (guncel - poz.alis_fiyati) * poz.adet
            poz_kz_str = f"+{poz_kz:.2f}" if poz_kz >= 0 else f"{poz_kz:.2f}"
            satirlar.append(
                f"  {sembol}: {poz.adet} adet | Alış: {poz.alis_fiyati} TL | "
                f"Güncel: {guncel} TL | K/Z: {poz_kz_str} TL"
            )
    else:
        satirlar.append("📌 Açık Pozisyon: Yok")

    satirlar.append(cizgi)
    satirlar.append(f"📋 İşlem Özeti:")
    satirlar.append(f"  Alım  : {len(alimlar)} işlem")
    satirlar.append(f"  Satım : {len(satimlar)} işlem")
    satirlar.append(f"  Gerçekleşen K/Z : {gerceklesen_kz:+.2f} TL")
    satirlar.append(f"  Toplam Komisyon  : {toplam_komisyon:.2f} TL")

    # İşlem detayları
    if portfolyo.islemler:
        satirlar.append(cizgi)
        satirlar.append("📝 İşlem Detayları:")
        for i in portfolyo.islemler:
            kz_str = f" | K/Z: {i.kar_zarar:+.2f} TL" if i.kar_zarar is not None else ""
            satirlar.append(
                f"  {i.zaman} | {i.tip:5s} | {i.sembol} | "
                f"{i.adet} adet @ {i.fiyat} TL{kz_str}"
            )

    satirlar += [cizgi, "⚠️ Bu rapor simülasyon verisidir, gerçek işlem değildir."]

    if html:
        govde = "".join(
            f"<p style='color:{'green' if '+' in s else ('red' if s.strip().startswith('-') else 'inherit')}'>"
            f"{s}</p>"
            for s in satirlar
        )
        return (
            '<html><body style="font-family:monospace;font-size:13px;'
            'background:#f9f9f9;padding:20px;">'
            f'{govde}</body></html>'
        )

    return "\n".join(satirlar)


def gun_sonu_email_gonder():
    """Seans kapanınca gün sonu raporunu e-posta ile gönderir."""
    if not EMAIL_AKTIF or not GMAIL_GONDEREN:
        log.info("[GÜN SONU] E-posta kapalı, rapor atlandı.")
        return

    toplam   = portfolyo.toplam_deger(_guncel_fiyatlar)
    kz       = portfolyo.kar_zarar_toplam(_guncel_fiyatlar)
    kz_emoji = "📈" if kz >= 0 else "📉"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = (
            f"{kz_emoji} BIST Simülasyon Raporu — "
            f"{datetime.now().strftime('%d.%m.%Y')} | "
            f"Toplam: {toplam:.2f} TL ({kz:+.2f} TL)"
        )
        msg["From"] = f"BIST Sinyal Botu <{GMAIL_GONDEREN}>"
        msg["To"]   = GMAIL_ALICI

        msg.attach(MIMEText(_gun_sonu_raporu_olustur(html=False), "plain", "utf-8"))
        msg.attach(MIMEText(_gun_sonu_raporu_olustur(html=True),  "html",  "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_GONDEREN, GMAIL_SIFRE)
            smtp.sendmail(GMAIL_GONDEREN, GMAIL_ALICI, msg.as_string())

        log.info(f"[GÜN SONU] Rapor e-posta ile gönderildi → {GMAIL_ALICI}")
    except Exception as e:
        log.error(f"[GÜN SONU] E-posta gönderilemedi: {e}")


def gun_sonu_telegram_gonder():
    """Gün sonu özetini Telegram'a gönderir (kısa versiyon)."""
    if not TELEGRAM_AKTIF or not TELEGRAM_TOKEN:
        return
    try:
        metin = _gun_sonu_raporu_olustur(html=False)
        # Telegram 4096 karakter sınırı — gerekirse kes
        if len(metin) > 4000:
            metin = metin[:4000] + "\n...(tam rapor e-posta ile gönderildi)"
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": metin}, timeout=10)
        log.info("[GÜN SONU] Rapor Telegram'a gönderildi.")
    except Exception as e:
        log.error(f"[GÜN SONU] Telegram raporu gönderilemedi: {e}")


def gun_sonu():
    """Seans kapandığında çalışır — rapor gönderir, portföyü sıfırlar."""
    log.info("[GÜN SONU] Seans kapandı, rapor hazırlanıyor...")
    log.info("\n" + _gun_sonu_raporu_olustur())

    gun_sonu_email_gonder()
    gun_sonu_telegram_gonder()

    # Ertesi güne sıfırla
    portfolyo.gun_baslangic = datetime.now().strftime("%Y-%m-%d")
    portfolyo.islemler.clear()
    log.info("[GÜN SONU] Portföy geçmişi temizlendi, yeni güne hazır.")


# ─────────────────────────────────────────
#  BİLDİRİM — TELEGRAM / EMAIL
# ─────────────────────────────────────────

def telegram_gonder(s: Sinyal) -> bool:
    if not TELEGRAM_AKTIF or not TELEGRAM_TOKEN:
        return False
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID,
                   "text": _metin_olustur(s), "parse_mode": "Markdown"}
        r = requests.post(url, json=payload, timeout=10)
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
        msg["Subject"] = (
            f"[BIST] {_emoji(s.tip, s.guc)} {s.sembol} — "
            f"{s.tip} ({s.guc}) @ {s.fiyat} TL"
        )
        msg["From"] = f"BIST Sinyal Botu <{GMAIL_GONDEREN}>"
        msg["To"]   = GMAIL_ALICI
        msg.attach(MIMEText(_metin_olustur(s, html=False), "plain", "utf-8"))
        msg.attach(MIMEText(_metin_olustur(s, html=True),  "html",  "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_GONDEREN, GMAIL_SIFRE)
            smtp.sendmail(GMAIL_GONDEREN, GMAIL_ALICI, msg.as_string())
        log.info(f"E-posta gönderildi: {s.sembol} → {GMAIL_ALICI}")
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
#  SEANS KONTROLÜ
# ─────────────────────────────────────────

def seans_acik() -> bool:
    simdi = datetime.now()
    if simdi.weekday() >= 5:
        return False
    return SEANS_BASLANGIC <= simdi.hour < SEANS_BITIS


son_sinyaller    = {sembol: None for sembol in HISSELER}
_seans_acikti    = False   # Seans kapanışını bir kez tetiklemek için


def sinyal_degisti_mi(sembol: str, yeni: Sinyal) -> bool:
    eski = son_sinyaller[sembol]
    if eski is None:
        return True
    return eski.tip != yeni.tip or eski.guc != yeni.guc


# ─────────────────────────────────────────
#  ANA DÖNGÜ
# ─────────────────────────────────────────

def kontrol_et():
    global _seans_acikti

    acik = seans_acik()

    # Seans yeni kapandıysa gün sonu raporunu tetikle
    if _seans_acikti and not acik:
        gun_sonu()

    _seans_acikti = acik

    if not acik:
        log.info("Seans kapalı, bekleniyor...")
        return

    log.info("Tarama başladı...")
    for sembol in HISSELER:
        veri = fiyat_cek(sembol)
        if not veri:
            log.warning(f"{sembol}: veri alınamadı, atlanıyor.")
            continue

        sinyal = sinyal_uret(sembol, veri)
        log.info(f"{sembol}: {sinyal.fiyat} TL → {sinyal.tip} ({sinyal.guc})")

        # Stop-loss her taramada kontrol et (sinyal değişmese de)
        stop_loss_kontrol(sembol, sinyal.fiyat)

        if sinyal.tip != "BEKLE" and sinyal_degisti_mi(sembol, sinyal):
            bildirim_gonder(sinyal)
            updateBalance(sinyal, veri)

        son_sinyaller[sembol] = sinyal

    log.info("Tarama tamamlandı.")


# ─────────────────────────────────────────
#  GİRİŞ NOKTASI
# ─────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  BIST Sinyal Botu v1.4 — Simülasyon Modu")
    log.info(f"  Başlangıç Bakiye : {portfolyo.baslangic_bakiye:,.0f} TL")
    log.info(f"  Pozisyon Büyüklük: %{int(portfolyo.POZISYON_BUYUKLUK_YUZDESI*100)}")
    log.info(f"  Komisyon Oranı   : %{KOMISYON_ORANI*100:.1f}")
    log.info(f"  Telegram : {'AÇIK' if TELEGRAM_AKTIF else 'KAPALI'}")
    log.info(f"  E-posta  : {'AÇIK' if EMAIL_AKTIF else 'KAPALI'}")
    log.info("=" * 50)

    kontrol_et()
    schedule.every(10).minutes.do(kontrol_et)

    log.info("Bot çalışıyor. Durdurmak için Ctrl+C")
    while True:
        schedule.run_pending()
        time.sleep(30)
