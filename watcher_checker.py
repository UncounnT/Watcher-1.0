#!/usr/bin/env python3
"""
watcher_checker.py

Jednopláťový skript (no chat integration) který:
- stáhne HTML z dané URL,
- vytáhne cenu, dostupnost a sekci "podrobnosti",
- uloží poslední stav do SQLite,
- porovná s předchozím stavem a vrátí (vytiskne) zda a co se změnilo.

Použití:
  python watcher_checker.py https://example.com/product/123

Závislosti:
  pip install requests beautifulsoup4
"""
from __future__ import annotations
import re
import json
import sqlite3
import argparse
from datetime import datetime
from typing import Optional, List, Dict, Any
import requests
from bs4 import BeautifulSoup
import sys
import logging

# ---- Konfigurace ----
DB_PATH = "watcher_state.db"
USER_AGENT = "watcher-checker/1.0 (+https://example.com)"
REQUEST_TIMEOUT = 15
# ----------------------

logging.basicConfig(level=logging.INFO, format="%(message)s")


# ---- DB helpers ----
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_cur = _conn.cursor()
_cur.execute(
    "CREATE TABLE IF NOT EXISTS page_state ("
    "url TEXT PRIMARY KEY, "
    "snapshot TEXT, "
    "checked_at TEXT"
    ")"
)
_conn.commit()


def load_state(url: str) -> Optional[Dict[str, Any]]:
    _cur.execute("SELECT snapshot FROM page_state WHERE url=?", (url,))
    row = _cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def save_state(url: str, snapshot: Dict[str, Any]) -> None:
    now = datetime.utcnow().isoformat() + "Z"
    snap_json = json.dumps(snapshot, ensure_ascii=False)
    _cur.execute(
        "INSERT INTO page_state(url, snapshot, checked_at) VALUES(?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET snapshot=excluded.snapshot, checked_at=excluded.checked_at",
        (url, snap_json, now),
    )
    _conn.commit()


# ---- Fetch + parse ----
def fetch_html(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    # Keep common currency patterns (Kč, CZK, €, $) and extract numeric part
    m = re.search(
        r'([€$]|Kč|CZK)?\s*([+-]?\d{1,3}(?:[ \xa0]\d{3})*(?:[.,]\d+)?)\s*(Kč|CZK|€|\$)?',
        t,
    )
    if not m:
        m2 = re.search(r'([+-]?\d[\d \xa0\.,]*)', t)
        if not m2:
            return t
        num = m2.group(1)
    else:
        num = m.group(2)
    num_norm = num.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        val = float(num_norm)
        return f"{val:.2f}"
    except Exception:
        return num_norm


def extract_price(soup: BeautifulSoup) -> Optional[str]:
    # 1) hledat elementy s itemprop="price" nebo meta price
    el = soup.select_one('[itemprop="price"], meta[itemprop="price"]')
    if el:
        if el.name == "meta":
            content = el.get("content")
            return normalize_price(content)
        else:
            return normalize_price(el.get_text(" ", strip=True))
    # 2) hledat text s měnou "Kč" nebo "CZK" nebo simboly
    text = soup.get_text(" ", strip=True)
    m = re.search(r'(\d{1,3}(?:[ \xa0]\d{3})*(?:[.,]\d+)?)[ ]*(Kč|CZK)', text)
    if m:
        return normalize_price(m.group(0))
    # 3) hledat elementy s třídou obsahující "price"
    price_el = soup.select_one('[class*="price"], [id*="price"]')
    if price_el:
        return normalize_price(price_el.get_text(" ", strip=True))
    return None


def extract_availability(soup: BeautifulSoup) -> Optional[str]:
    # 1) podle itemprop availability
    el = soup.select_one('[itemprop="availability"], [class*="availability"], [id*="availability"]')
    if el:
        return el.get_text(" ", strip=True)
    # 2) hledat textové fráze
    text = soup.get_text("\n", strip=True)
    # hledej "Dostupnost:" nebo slova "Skladem", "Vyprodáno", "Na dotaz", "Dostupné"
    m = re.search(r'(Dostupnost\s*[:\-]?\s*([^\n\r]+))', text, flags=re.IGNORECASE)
    if m:
        return m.group(2).strip()
    for keyword in ["Skladem", "Vyprodáno", "Není skladem", "Do týdne", "Na objednávku", "Dostupné", "Available", "Out of stock", "In stock"]:
        if re.search(re.escape(keyword), text, flags=re.IGNORECASE):
            mm = re.search(r'.{0,40}' + re.escape(keyword) + r'.{0,40}', text, flags=re.IGNORECASE)
            return mm.group(0).strip() if mm else keyword
    return None


def extract_details(soup: BeautifulSoup) -> List[str]:
    details: List[str] = []
    # Hledat nadpis "Podrobnosti" a následné <ul>/<ol> nebo odstavce
    for header_tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        hdrs = soup.find_all(header_tag, string=re.compile(r'podrobnost', re.IGNORECASE))
        if hdrs:
            for hdr in hdrs:
                next_el = hdr.find_next_sibling()
                if next_el and next_el.name in ("ul", "ol"):
                    for li in next_el.find_all("li"):
                        details.append(li.get_text(" ", strip=True))
                    if details:
                        return details
                # fallback: pár následujících siblingů
                sib = hdr
                count = 0
                while count < 8:
                    sib = sib.find_next_sibling()
                    if not sib:
                        break
                    txt = sib.get_text(" ", strip=True)
                    if txt:
                        details.append(txt)
                    count += 1
                if details:
                    return details
    # fallback: hledat sekce se slovy "Specifikace" nebo "Specification"
    spec_headers = soup.find_all(string=re.compile(r'(specifikace|specification|parametr|parameters)', re.IGNORECASE))
    if spec_headers:
        for sh in spec_headers[:3]:
            parent = sh.parent
            ul = parent.find_next("ul")
            if ul:
                for li in ul.find_all("li"):
                    details.append(li.get_text(" ", strip=True))
                if details:
                    return details
    # poslední fallback: všechny <li> na stránce (omezeně)
    for li in soup.find_all("li")[:50]:
        text = li.get_text(" ", strip=True)
        if text:
            details.append(text)
    # deduplikace a trim
    seen = set()
    out: List[str] = []
    for d in details:
        d2 = " ".join(d.split())
        if d2 and d2 not in seen:
            seen.add(d2)
            out.append(d2)
    return out


# ---- Compare & summarize ----
def summarize_changes(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    Vrací dict s klíči:
      changed: bool
      changes: list[str] (lidsky čitelné)
      old: {...}
      new: {...}
    """
    changes: List[str] = []
    changed = False
    if old is None:
        changes.append("Žádný předchozí záznam — uloženo jako výchozí stav.")
        changed = True
    else:
        # price
        old_price = old.get("price")
        new_price = new.get("price")
        if (old_price or "") != (new_price or ""):
            changed = True
            changes.append(f"Cena: {old_price} → {new_price}")

            # pokud obě ceny číselné, spočti procenta
            try:
                a = float(old_price) if old_price is not None else None
                b = float(new_price) if new_price is not None else None
                if a is not None and b is not None and a != 0:
                    pct = (b - a) / a * 100.0
                    changes[-1] += f" ({pct:+.2f}%)"
            except Exception:
                pass

        # availability
        if (old.get("availability") or "") != (new.get("availability") or ""):
            changed = True
            changes.append(f"Dostupnost: {old.get('availability')} → {new.get('availability')}")

        # details: jednoduché diff (added/removed)
        old_det = old.get("details", []) if old else []
        new_det = new.get("details", [])
        added = [d for d in new_det if d not in old_det]
        removed = [d for d in old_det if d not in new_det]
        if added:
            changed = True
            changes.append("Přidáno v podrobnostech: " + "; ".join(added[:10]))
        if removed:
            changed = True
            changes.append("Odebráno v podrobnostech: " + "; ".join(removed[:10]))

    return {"changed": changed, "changes": changes, "old": old, "new": new}


# ---- Main check function ----
def check_page(url: str, save_state: bool = True) -> Dict[str, Any]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "lxml")

    price = extract_price(soup)
    availability = extract_availability(soup)
    details = extract_details(soup)

    new_snapshot = {"price": price, "availability": availability, "details": details, "checked_at": datetime.utcnow().isoformat() + "Z"}
    old_snapshot = load_state(url)

    summary = summarize_changes(old_snapshot, new_snapshot)

    if save_state:
        save_state(url, new_snapshot)

    return summary


# ---- CLI ----
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Watcher: zjistí změny na stránce (cena, dostupnost, podrobnosti).")
    p.add_argument("urls", nargs="+", help="URL(y) ke zkontrolování")
    p.add_argument("--no-save", action="store_true", help="nezapisovat nový stav do DB (pouze porovnat)")
    p.add_argument("--json", action="store_true", help="vypíše strojově čitelný JSON výstup")
    args = p.parse_args()

    results = {}
    for url in args.urls:
        try:
            logging.info(f"Kontrola: {url}")
            res = check_page(url, save_state=not args.no_save)
            results[url] = res
            if args.json:
                # akumulovat, vypsat na konci
                continue
            # lidské shrnutí
            if res["changed"]:
                logging.info("Změny detekovány:")
                for c in res["changes"]:
                    logging.info(" - %s", c)
            else:
                logging.info("Žádné změny detekovány.")
            logging.info("")  # newline
        except Exception as e:
            logging.error("Chyba při zpracování %s: %s", url, e)
            results[url] = {"error": str(e)}

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
