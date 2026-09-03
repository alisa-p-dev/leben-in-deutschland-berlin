import hashlib
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SERVICE_URL = "https://service.berlin.de/dienstleistung/351180/"
PANKOW_URL = "https://www.berlin.de/vhs/volkshochschulen/pankow/service/einbuergerungstest-vhs-pankow-1463977.php"
STATE_FILE = "state.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
}


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


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


def extract_provider_links(page):
    """Read the current ServicePortal DOM after JavaScript has rendered it."""
    links = page.locator('a[href*="/terminvereinbarung/termin/provider/"]').evaluate_all(
        "els => els.map(a => ({text: a.innerText, href: a.href}))"
    )

    result = []
    seen = set()
    for item in links:
        href = item["href"]
        if "351180" not in href or href in seen:
            continue
        seen.add(href)
        result.append((normalize(item["text"]) or "Berliner VHS", href))
    return result


def page_has_real_slot(page):
    """Detect an actual selectable appointment, not a generic booking button."""
    text = normalize(page.locator("body").inner_text()).lower()

    no_slot_markers = [
        "keine freien termine",
        "keine termine verfügbar",
        "keine termine vorhanden",
        "zurzeit leider ausgebucht",
        "ausgebucht",
    ]
    if any(marker in text for marker in no_slot_markers):
        return False

    # The appointment portal exposes actual date/time choices as links or buttons.
    candidates = page.locator("a, button").all_inner_texts()
    for raw in candidates:
        label = normalize(raw).lower()
        if not label:
            continue
        if re.search(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", label):
            return True
        if re.search(r"\b\d{1,2}:\d{2}\b", label) and re.search(r"termin|uhr", label):
            return True
        if re.search(r"termin\s+(auswählen|auswaehlen|verfügbar|verfuegbar)", label):
            return True

    # Also inspect hrefs for appointment selection pages with date/time parameters.
    hrefs = page.locator('a[href*="/terminvereinbarung/termin/"]').evaluate_all(
        "els => els.map(a => a.href)"
    )
    for href in hrefs:
        if "/provider/" not in href and ("datum" in href.lower() or "date" in href.lower()):
            return True

    return False


def check_with_browser():
    found = []
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2000)
            provider_links = extract_provider_links(page)
        except Exception as exc:
            provider_links = []
            errors.append(f"Central ServicePortal: {exc}")

        for label, url in provider_links:
            try:
                ppage = context.new_page()
                ppage.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    ppage.wait_for_load_state("networkidle", timeout=20000)
                except PlaywrightTimeoutError:
                    pass
                ppage.wait_for_timeout(1500)
                if page_has_real_slot(ppage):
                    found.append((label, ppage.url))
                ppage.close()
            except Exception as exc:
                errors.append(f"{label}: {exc}")

        browser.close()

    return provider_links, found, errors


def check_pankow():
    """Pankow uses a separate registration process; detect changes on its official page."""
    try:
        r = requests.get(PANKOW_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        ptext = normalize(BeautifulSoup(r.text, "html.parser").get_text(" "))
        new_hash = hashlib.sha256(ptext.encode("utf-8")).hexdigest()
        old_hash = ""
        if os.path.exists("pankow_state.txt"):
            with open("pankow_state.txt", encoding="utf-8") as f:
                old_hash = f.read().strip()
        with open("pankow_state.txt", "w", encoding="utf-8") as f:
            f.write(new_hash)
        if old_hash and old_hash != new_hash:
            return [("VHS Pankow – offizielle Seite geändert", PANKOW_URL)], None
        return [], None
    except Exception as exc:
        return [], f"Pankow: {exc}"


def main():
    provider_links, found, errors = check_with_browser()

    pankow_found, pankow_error = check_pankow()
    found.extend(pankow_found)
    if pankow_error:
        errors.append(pankow_error)

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
