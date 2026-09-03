import hashlib
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SERVICE_URL = "https://service.berlin.de/dienstleistung/351180/"
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


def extract_provider_links(html, base):
    """The Berlin ServicePortal now uses /termin/provider/... links for the location calendars."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if "/terminvereinbarung/termin/provider/" in href and "351180" in href:
            label = normalize(a.get_text(" "))
            links.append((label, href))

    seen = set()
    result = []
    for item in links:
        if item[1] not in seen:
            seen.add(item[1])
            result.append(item)
    return result


def has_real_slot(html):
    """Return True only when the provider page exposes a selectable date/time, not merely a booking button."""
    soup = BeautifulSoup(html, "html.parser")
    text = normalize(soup.get_text(" ")).lower()

    no_slot_markers = [
        "keine freien termine",
        "keine termine verfügbar",
        "keine termine vorhanden",
        "zurzeit leider ausgebucht",
        "ausgebucht",
    ]
    if any(marker in text for marker in no_slot_markers):
        return False

    # Appointment calendars normally contain a link away from the provider page
    # to a day/time selection page. A plain "Termin buchen" link is NOT enough.
    for a in soup.find_all("a", href=True):
        href = urljoin(SERVICE_URL, a["href"])
        if "/terminvereinbarung/termin/" in href and "/provider/" not in href:
            return True

    # Also accept explicit selectable appointment wording.
    return bool(re.search(r"termin\s+(auswählen|auswaehlen|verfügbar|verfuegbar)", text, re.I))


def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing Telegram secrets")

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    r.raise_for_status()


def main():
    found = []
    errors = []

    try:
        central = get(SERVICE_URL)
        provider_links = extract_provider_links(central.text, central.url)
    except Exception as exc:
        provider_links = []
        errors.append(f"Central ServicePortal: {exc}")

    for label, url in provider_links:
        try:
            r = get(url)
            if has_real_slot(r.text):
                found.append((label or "Berliner VHS", url))
        except Exception as exc:
            errors.append(f"{label or url}: {exc}")

    # Pankow has a separate registration process. We monitor its official page for changes.
    try:
        p = get(PANKOW_URL)
        ptext = normalize(BeautifulSoup(p.text, "html.parser").get_text(" "))
        pankow_hash = hashlib.sha256(ptext.encode("utf-8")).hexdigest()
        old_pankow = ""
        if os.path.exists("pankow_state.txt"):
            with open("pankow_state.txt", encoding="utf-8") as f:
                old_pankow = f.read().strip()
        if old_pankow and old_pankow != pankow_hash:
            found.append(("VHS Pankow – offizielle Seite geändert", PANKOW_URL))
        with open("pankow_state.txt", "w", encoding="utf-8") as f:
            f.write(pankow_hash)
    except Exception as exc:
        errors.append(f"Pankow: {exc}")

    signature = hashlib.sha256(
        "\n".join(f"{name}|{url}" for name, url in found).encode("utf-8")
    ).hexdigest()
    old_signature = ""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            old_signature = f.read().strip()

    if found and signature != old_signature:
        lines = ["🚨 <b>Termin gefunden!</b>", ""]
        for name, url in found:
            lines.append(f"📍 <b>{name}</b>")
            lines.append(f'🔗 <a href="{url}">Öffnen / buchen</a>')
            if "Pankow" in name:
                lines.append("ℹ️ Pankow hat ein eigenes Anmeldeverfahren – bitte die Seite sofort prüfen.")
            lines.append("")
        telegram("\n".join(lines))

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(signature if found else "")

    if errors:
        print("Warnings:")
        for error in errors:
            print(error)
    print(f"Checked {len(provider_links)} VHS provider calendars; free/changed: {len(found)}")


if __name__ == "__main__":
    main()
