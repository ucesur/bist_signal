"""
BIST Hisse Senedi Sinyal Botu
==============================
Hisseler  : KCAER, ECILC, TTRAK
Periyot   : 1 Saatlik
Bildirim  : Telegram + Gmail (smtplib)
Veri      : Bigpara (~15 dk gecikmeli, kayıt gerekmez)

Kurulum:
  pip install requests schedule
  (smtplib Python ile birlikte gelir, ek kurulum gerekmez)

Gmail Uygulama Şifresi Alma:
  1. Google Hesabı → Güvenlik → 2 Adımlı Doğrulama (açık olmalı)
  2. Güvenlik → Uygulama Şifreleri → "Posta" / "Windows Bilgisayarı"
  3. Oluşturulan 16 haneli şifreyi GMAIL_SIFRE alanına girin
  4. Normal Gmail şifrenizi KULLANMAYIN

Telegram Bot Kurulumu:
  1. Telegram'da @BotFather'a yazın → /newbot
  2. Bot adı ve kullanıcı adı girin → TOKEN alın
  3. @userinfobot'a yazın → Chat ID alın
"""

import requests
import schedule
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

# ─────────────────────────────────────────
#  AYARLAR — Buraya kendi bilgilerinizi girin
# ─────────────────────────────────────────

from dotenv import load_dotenv
import os
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_GONDEREN   = os.getenv("GMAIL_GONDEREN", "")
GMAIL_SIFRE      = os.getenv("GMAIL_SIFRE", "")
GMAIL_ALICI      = os.getenv("GMAIL_ALICI", "")

# Bildirim kanalları — istediğinizi True/False yapın
TELEGRAM_AKTIF  = False
EMAIL_AKTIF     = True

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


def fiyat_cek(sembol: str) -> Optional[dict]:
    """Bigpara'dan hisse verisini çeker (~15 dk gecikmeli)."""
    url = f"https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/{sembol}"
    try:
        r = requests.get(url, headers=BIGPARA_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {}).get("hisseYuzeysel", {})
        if not data:
            print(f"[UYARI] {sembol}: Bigpara boş veri döndürdü.")
            return None

        fiyat   = float(str(data.get("kapanis") or data.get("alisFiyati") or "0").replace(",", "."))
        hacim   = _hacim_parse(data.get("islemHacmi") or "0")
        degisim = float(str(data.get("yuzde") or "0").replace(",", ".").replace("%", ""))

        return {
            "sembol":        sembol,
            "fiyat":         fiyat,
            "hacim":         hacim,
            "hacim_ort":     _hacim_ort_guncelle(sembol, hacim),
            "degisim_yuzde": degisim,
            "zaman":         datetime.now().strftime("%H:%M"),
        }
    except requests.exceptions.RequestException as e:
        print(f"[HATA] {sembol} ağ hatası: {e}")
        return None
    except (KeyError, ValueError, TypeError) as e:
        print(f"[HATA] {sembol} veri hatası: {e}")
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
    """
    Hem Telegram hem e-posta için ortak içerik üretir.
    html=True  → HTML e-posta gövdesi
    html=False → Telegram Markdown metni
    """
    emoji      = _emoji(s.tip, s.guc)
    hacim_str  = "✅ Yüksek hacim" if s.hacim_ok else "⚠️ Düşük hacim"

    satirlar = [
        f"{emoji} {s.sembol} — {s.ad}",
        "─" * 30,
        f"Fiyat    : {s.fiyat} TL",
        f"Sinyal   : {s.tip} ({s.guc})",
        f"Hacim    : {hacim_str}",
        f"Neden    : {s.neden}",
    ]
    if s.stop:    satirlar.append(f"Stop-Loss: {s.stop} TL")
    if s.hedef_1: satirlar.append(f"Hedef 1  : {s.hedef_1} TL")
    if s.hedef_2: satirlar.append(f"Hedef 2  : {s.hedef_2} TL")
    if s.hedef_3: satirlar.append(f"Hedef 3  : {s.hedef_3} TL")
    satirlar += ["─" * 30, f"Saat: {s.zaman}",
                 "⚠️ Bu mesaj yatırım tavsiyesi değildir."]

    if html:
        govde = "".join(f"<p>{satir}</p>" for satir in satirlar)
        return f"""
        <html><body style="font-family:monospace; font-size:14px;">
        {govde}
        </body></html>
        """

    # Telegram Markdown
    baslik = f"*{s.sembol} — {s.ad}*"
    icerik = "\n".join(satirlar[1:])
    return f"{emoji} {baslik}\n{icerik}"


# ─────────────────────────────────────────
#  BİLDİRİM — TELEGRAM
# ─────────────────────────────────────────

def telegram_gonder(s: Sinyal) -> bool:
    """Telegram'a sinyal mesajı gönderir."""
    if not TELEGRAM_AKTIF:
        return False
    if TELEGRAM_TOKEN == "BURAYA_BOT_TOKEN_YAZIN":
        print(f"[DEMO-TELEGRAM]\n{_metin_olustur(s)}\n")
        return True
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID,
                   "text": _metin_olustur(s),
                   "parse_mode": "Markdown"}
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"[✅] Telegram gönderildi: {s.sembol}")
            return True
        print(f"[❌] Telegram hatası {r.status_code}: {r.text}")
        return False
    except Exception as e:
        print(f"[❌] Telegram bağlantı hatası: {e}")
        return False


# ─────────────────────────────────────────
#  BİLDİRİM — E-POSTA (Gmail / smtplib)
# ─────────────────────────────────────────

def send_email(s: Sinyal) -> bool:
    """
    Gmail SMTP üzerinden HTML e-posta gönderir.

    Gereksinimler:
      - Google hesabında 2 Adımlı Doğrulama açık olmalı
      - GMAIL_SIFRE alanına normal şifre değil, 16 haneli
        Uygulama Şifresi girilmeli (Google Hesabı → Güvenlik
        → Uygulama Şifreleri)
    """
    if not EMAIL_AKTIF:
        return False
    if GMAIL_GONDEREN == "gonderen@gmail.com":
        print(f"[DEMO-EMAIL] Konu: BIST Sinyal | {s.sembol} {s.tip} ({s.guc})\n")
        return True
    try:
        # ── Mesajı oluştur ──────────────────────────
        msg = MIMEMultipart("alternative")
        msg["Subject"] = (
            f"[BIST] {_emoji(s.tip, s.guc)} {s.sembol} — "
            f"{s.tip} ({s.guc}) @ {s.fiyat} TL"
        )
        msg["From"]    = f"BIST Sinyal Botu <{GMAIL_GONDEREN}>"
        msg["To"]      = GMAIL_ALICI

        # Düz metin (yedek)
        duz_metin = _metin_olustur(s, html=False)
        msg.attach(MIMEText(duz_metin, "plain", "utf-8"))

        # HTML gövde
        html_metin = _metin_olustur(s, html=True)
        msg.attach(MIMEText(html_metin, "html", "utf-8"))

        # ── Gmail SMTP bağlantısı ────────────────────
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_GONDEREN, GMAIL_SIFRE)
            smtp.sendmail(GMAIL_GONDEREN, GMAIL_ALICI, msg.as_string())

        print(f"[✅] E-posta gönderildi: {s.sembol} → {GMAIL_ALICI}")
        return True

    except smtplib.SMTPAuthenticationError:
        print("[❌] Gmail kimlik doğrulama hatası!")
        print("     → Uygulama Şifresi kullandığınızdan emin olun.")
        print("     → Google Hesabı → Güvenlik → Uygulama Şifreleri")
        return False
    except smtplib.SMTPException as e:
        print(f"[❌] SMTP hatası: {e}")
        return False
    except Exception as e:
        print(f"[❌] E-posta gönderilemedi: {e}")
        return False


def bildirim_gonder(s: Sinyal):
    """Aktif kanalların tamamına bildirim gönderir."""
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


son_sinyaller = {sembol: None for sembol in HISSELER}


def sinyal_degisti_mi(sembol: str, yeni: Sinyal) -> bool:
    eski = son_sinyaller[sembol]
    if eski is None:
        return True
    return eski.tip != yeni.tip or eski.guc != yeni.guc


# ─────────────────────────────────────────
#  ANA DÖNGÜ
# ─────────────────────────────────────────

def kontrol_et():
    """Her 10 dakikada bir çalışır — tüm hisseleri tarar."""
    if not seans_acik():
        print(f"[{datetime.now().strftime('%H:%M')}] Seans kapalı, bekleniyor...")
        return

    print(f"\n[{datetime.now().strftime('%H:%M')}] Tarama başladı...")

    for sembol in HISSELER:
        veri = fiyat_cek(sembol)
        if not veri:
            print(f"  {sembol}: veri alınamadı, atlanıyor.")
            continue

        sinyal = sinyal_uret(sembol, veri)
        print(f"  {sembol}: {sinyal.fiyat} TL → {sinyal.tip} ({sinyal.guc})")

        if sinyal.tip != "BEKLE" and sinyal_degisti_mi(sembol, sinyal):
            bildirim_gonder(sinyal)

        son_sinyaller[sembol] = sinyal

    print(f"[{datetime.now().strftime('%H:%M')}] Tarama tamamlandı.\n")

# ─────────────────────────────────────────
#  GİRİŞ NOKTASI
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  BIST Sinyal Botu v1.2")
    print("  Hisseler : KCAER, ECILC, TTRAK")
    print("  Periyot  : 10 dakika")
    print("  Veri     : Bigpara (~15 dk gecikmeli)")
    print(f"  Telegram : {'AÇIK' if TELEGRAM_AKTIF else 'KAPALI'}")
    print(f"  E-posta  : {'AÇIK' if EMAIL_AKTIF else 'KAPALI'}")
    print("=" * 50)

    kontrol_et()

    schedule.every(10).minutes.do(kontrol_et)

    print("\n[✅] Bot çalışıyor. Durdurmak için Ctrl+C\n")
    while True:
        schedule.run_pending()
        time.sleep(30)
