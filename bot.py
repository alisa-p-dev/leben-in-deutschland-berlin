import hashlib
import os
import re
import subprocess
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SERVICE_URL = "https://service.berlin.de/dienstleistung/351180/"
VHS_INFO_URL = "https://www.berlin.de/vhs/deutscher-einbuergerungstest/"
PANKOW_URL = "https://www.berlin.de/vhs/volkshochschulen/pankow/service/einbuergerungstest-vhs-pankow-1463977.php"
STATE_FILE = "state.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Berlin-LiD-Terminwatch/1.0; +https://github.com/)"
}


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_booking_links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if "terminvereinbarung/termin/tag.php" in href and "anliegen%5B%5D=351180" in href:
            links.append((normalize(a.get_text(" ")), href))
    # Keep unique URLs while preserving order.
    seen = set()
    return [(name, url) for name, url in links if not (url in seen or seen.add(url))]


def page_has_available_slot(text):
    t = normalize(text).lower()
    no_slot_markers = [
        "keine freien termine",
        "keine termine verfügbar",
        "keine termine vorhanden",
        "ausgebucht",
        "zurzeit leider ausgebucht",
    ]
    if any(m in t for m in no_slot_markers):
        return False
    # The ServicePortal uses date/time entries around selectable appointment links.
    return bool(re.search(r"\b(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b.*\b\d{1,2}:\d{2}\b", t, re.I | re.S)) or "termin auswählen" in t or "termin buchen" in t


def main():
    found = []
    errors = []

    try:
        central = get(SERVICE_URL)
        booking_links = extract_booking_links(central.text, central.url)
    except Exception as exc:
        booking_links = []
        errors.append(f"Central ServicePortal: {exc}")

    # If the central page exposes location booking links, check each location page.
    for label, url in booking_links:
        try:
            r = get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            text = normalize(soup.get_text(" "))
            if page_has_available_slot(text):
                found.append((label or "Berliner VHS", url, text[:1200]))
        except Exception as exc:
            errors.append(f"{label or url}: {exc}")

    # Pankow has its own process and does not use the central booking page.
    try:
        p = get(PANKOW_URL)
        ptext = normalize(BeautifulSoup(p.text, "html.parser").get_text(" "))
        # Notify on a change on the official Pankow page; this is where opening dates are announced.
        pankow_hash = hashlib.sha256(ptext.encode("utf-8")).hexdigest()
        old = ""
        if os.path.exists("pankow_state.txt"):
            old = open("pankow_state.txt", encoding="utf-8").read().strip()
        if old and old != pankow_hash:
            found.append(("VHS Pankow (Seite geändert)", PANKOW_URL, ptext[:1400]))
        with open("pankow_state.txt", "w", encoding="utf-8") as f:
            f.write(pankow_hash)
    except Exception as exc:
        errors.append(f"Pankow: {exc}")

    # De-duplicate against the previous notification state.
    signature = hashlib.sha256("\n".join(f"{a}|{b}" for a, b, _ in found).encode()).hexdigest()
    old_signature = ""
    if os.path.exists(STATE_FILE):
        old_signature = open(STATE_FILE, encoding="utf-8").read().strip()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing Telegram secrets")

    if found and signature != old_signature:
        lines = ["🚨 <b>Termin gefunden / Seite geändert!</b>", ""]
        for name, url, excerpt in found:
            lines.append(f"📍 <b>{name}</b>")
            lines.append(f"🔗 <a href=\"{url}\">Öffnen / buchen</a>")
            if "Seite geändert" in name:
                lines.append("ℹ️ VHS Pankow: Die offizielle Seite wurde geändert – bitte sofort prüfen.")
            lines.append("")
        message = "\n".join(lines)
        tg = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(tg, data={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=30)
        resp.raise_for_status()

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(signature if found else "")

    if errors:
        print("Warnings:")
        for e in errors:
            print(e)
    print(f"Checked {len(booking_links)} central booking links; alerts: {len(found)}")


if __name__ == "__main__":
    main()
