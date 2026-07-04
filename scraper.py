#!/usr/bin/env python3
"""
Price Tracker — RealAdvisor scraper
-------------------------------------------
Fetches search-result pages from realadvisor.ch, extracts listings (price,
address, agency, url), and logs them to Supabase so price drops over time
become visible.

IMPORTANT — read before relying on this:
This was written from a manual inspection of RealAdvisor's rendered output
(fetched once, structure confirmed at build time), NOT from live raw HTML
parsing during development — I don't have network access to realadvisor.ch
from where this was built. Run it in DIAGNOSTIC MODE first (see bottom of
file) and eyeball a few parsed listings against the real page before you
trust it or schedule it.

Setup:
    pip install requests beautifulsoup4 --break-system-packages

Env vars required:
    SUPABASE_URL       e.g. https://hzbwlqyhklwfxjbcramx.supabase.co
    SUPABASE_KEY       service_role key (server-side use only, never expose client-side)

Usage:
    python scraper.py                 # runs all active watchlist entries, writes to Supabase
    python scraper.py --diagnose 3    # fetches watchlist id 3, PRINTS parsed listings, writes nothing
"""

import os
import re
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

HEADERS_SUPABASE = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

REQUEST_DELAY_SECONDS = 3  # be polite - one request every few seconds, no concurrency


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_realadvisor(html: str):
    """
    Extract listings from a RealAdvisor search-results page.

    Strategy:
      1. Try structured JSON-LD (schema.org) blocks first — most robust,
         survives redesigns.
      2. Fall back to regex over listing-card anchor tags, based on the
         pattern: <a href=".../acheter/<type>/<slug>-<ID>"> containing
         address, price (CHF ...), m², and agency name as plain text.

    Returns a list of dicts: external_id, url, title, address, agency,
    currency, price, price_per_sqm
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # --- Strategy 1: JSON-LD ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") in ("Product", "RealEstateListing", "Offer"):
                url = item.get("url") or item.get("@id")
                offers = item.get("offers", item)
                price = offers.get("price") if isinstance(offers, dict) else None
                currency = offers.get("priceCurrency") if isinstance(offers, dict) else None
                if url and price:
                    results.append({
                        "external_id": extract_external_id(url),
                        "url": url,
                        "title": item.get("name"),
                        "address": None,
                        "agency": None,
                        "currency": currency or "CHF",
                        "price": safe_float(price),
                        "price_per_sqm": None,
                    })

    if results:
        return results

    # --- Strategy 2: anchor-tag regex fallback ---
    anchors = soup.find_all("a", href=re.compile(r"/acheter/[a-z]+/"))
    seen_urls = set()
    for a in anchors:
        href = a.get("href", "")
        if href in seen_urls:
            continue
        seen_urls.add(href)

        text = a.get_text(separator=" ", strip=True)
        if not text or "CHF" not in text and "Prix sur demande" not in text:
            continue

        url = href if href.startswith("http") else f"https://realadvisor.ch{href}"
        external_id = extract_external_id(url)

        # Price: first "CHF x'xxx'xxx" occurrence
        price_match = re.search(r"CHF\s*([\d']+)(?!\s*m²)", text)
        price = safe_float(price_match.group(1).replace("'", "")) if price_match else None

        # Price per sqm: pattern "CHF x'xxx m²"
        sqm_match = re.search(r"CHF\s*([\d']+)\s*m²", text)
        price_per_sqm = safe_float(sqm_match.group(1).replace("'", "")) if sqm_match else None

        # Address: text between " - " after "à vendre" and the next " - " or digit block
        addr_match = re.search(r"à vendre\s*-\s*([^-]+?)(?:\s*-\s*Photo|\s*\d+\s*pi[eè]ces|$)", text)
        address = addr_match.group(1).strip() if addr_match else None

        # Agency: heuristic — often appears right after the address block, repeated.
        # Left as None here; refine once you've eyeballed diagnostic output.
        agency = None

        if price is None and "Prix sur demande" not in text:
            continue  # couldn't parse a usable price, skip rather than store garbage

        results.append({
            "external_id": external_id,
            "url": url,
            "title": text[:120],
            "address": address,
            "agency": agency,
            "currency": "CHF",
            "price": price,
            "price_per_sqm": price_per_sqm,
        })

    return results


def parse_bellesdemeures(html: str):
    """
    Extract listings from a bellesdemeures.com search-results page.

    Confirmed structure (fetched live during development): each listing is
    a single <a href="/annonces/vente/tt-.../{listing_id}/"> wrapping text
    of the form:

      "Message envoyé le Villa 10 Pièces • 735 m² Cannes 28 900 000 €
       À partir de 167607€ /mois Pourquoi est-ce que je vois cette
       publicité? ... [description]"

    or, when the seller withholds price:

      "... Cannes Prix sur demande ..."

    Returns a list of dicts: external_id, url, title, address, agency,
    currency, price, price_per_sqm (always None here - not shown on cards)
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    card_re = re.compile(
        r"Message envoyé le\s+(?P<type>.+?)\s*"
        r"(?:(?P<rooms>\d+)\s*Pi[eè]ces?\s*[•·]\s*)?"
        r"(?P<surface>[\d,]+)\s*m[²2]\s*"
        r"(?P<location>[^\d€]+?)\s+"
        r"(?:(?P<price>[\d\s\u202f\u00a0]{5,}?)\s*€|Prix sur demande)"
        r"(?:\s*À partir de[\d\s\u202f\u00a0]+€\s*/mois)?"
        r"\s*Pourquoi est-ce que je vois cette publicit",
        re.UNICODE,
    )

    anchors = soup.find_all("a", href=re.compile(r"/annonces/vente/"))
    seen_urls = set()

    for a in anchors:
        href = a.get("href", "")
        if href in seen_urls or not href:
            continue
        seen_urls.add(href)

        url = href if href.startswith("http") else f"https://www.bellesdemeures.com{href}"
        external_id = extract_external_id(url)

        text = a.get_text(separator=" ", strip=True)
        m = card_re.search(text)
        if not m:
            continue  # doesn't match the expected card shape - skip rather than guess

        price = safe_float(m.group("price").replace(" ", "").replace("\u202f", "")
                            .replace("\u00a0", "")) if m.group("price") else None
        location = m.group("location").strip(" -") if m.group("location") else None
        prop_type = m.group("type").strip() if m.group("type") else None

        # Agency name: confirmed to sit as a sibling element immediately
        # after the listing anchor closes (e.g. "Douglas Elliman Cannes"
        # appearing right after the card's link, outside it). Walk forward
        # through siblings until we hit real text that isn't the price/
        # description we already have.
        agency = None
        node = a.next_sibling
        hops = 0
        while node is not None and hops < 6:
            hops += 1
            candidate = node.get_text(strip=True) if hasattr(node, "get_text") \
                else str(node).strip()
            if candidate and "€" not in candidate and len(candidate) < 80:
                agency = candidate
                break
            node = node.next_sibling

        results.append({
            "external_id": external_id,
            "url": url.split("#")[0],
            "title": prop_type,
            "address": location,
            "agency": agency,
            "currency": "EUR",
            "price": price,
            "price_per_sqm": None,
        })

    return results


def paginate_url(base_url: str, page: int, source: str) -> str:
    """Different sites paginate differently."""
    if page <= 1:
        return base_url
    if source == "bellesdemeures":
        # appends /{page}/ to the path
        return f"{base_url.rstrip('/')}/{page}/"
    if source in ("homegate", "kyero", "jamesedition"):
        # homegate uses ep=, kyero and jamesedition use page=
        param = "ep" if source == "homegate" else "page"
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}{param}={page}"
    return base_url


def parse_homegate(html: str):
    """
    Extract listings from a homegate.ch search-results page.

    Confirmed structure (fetched live during development): each listing is
    a single <a href="https://www.homegate.ch/buy/{id}"> wrapping text like:

      "CHF 6,190,000.– Premium 7 rooms 372m² living space
       Genève, 1223 Cologny Villas Connétable | Cologny [description...]
       Travel time-"

    Returns a list of dicts: external_id, url, title, address, agency,
    currency, price, price_per_sqm (not shown on cards - always None)
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    card_re = re.compile(
        r"CHF\s*([\d,]+)\.–.*?"                              # price
        r"(?:\*\*([\d.]+)\*\*\s*rooms)?\s*"                  # optional room count
        r"(?:\*\*(\d+)m²\*\*\s*living space)?\s*"            # optional surface
        r"([^\d]{3,80}?\d{4}\s+[^\n]{2,40}?)"                # location (postal code + city)
        r"(?=[A-ZÀ-Ü]|$)",                                   # up to the next capitalised title
        re.UNICODE,
    )

    anchors = soup.find_all("a", href=re.compile(r"homegate\.ch/buy/\d+"))
    seen_urls = set()

    for a in anchors:
        href = a.get("href", "")
        if href in seen_urls or not href:
            continue
        seen_urls.add(href)

        text = a.get_text(separator=" ", strip=True)
        if "CHF" not in text:
            continue

        m = card_re.search(text)
        if not m:
            continue

        price = safe_float(m.group(1))
        location = m.group(4).strip() if m.group(4) else None

        results.append({
            "external_id": extract_external_id(href),
            "url": href,
            "title": "Villa/Maison",  # room/type text is inconsistent enough to skip for now
            "address": location,
            "agency": None,
            "currency": "CHF",
            "price": price,
            "price_per_sqm": None,
        })

    return results





def parse_kyero(html: str):
    """
    Extract listings from a kyero.com search-results page (Costa del Sol /
    Spain). Confirmed structure (fetched live during development): each
    listing is a single <a href="https://www.kyero.com/en/property/{id}-..."
    wrapping text like:

      "Featured Villa in Marbella, Malaga € 5,875,000 This contemporary
       villa is located in Marbella East... pool Pool garden Garden
       parking Parking Golden Properties * 5 * 5 * 837 m²"

    Returns a list of dicts: external_id, url, title, address, agency,
    currency, price, price_per_sqm (not shown directly - always None)
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    card_re = re.compile(
        r"(?:Featured|Near beach|New build)?\s*"
        r"(?P<type>Villa|Apartment|Town\s?[Hh]ouse|Country\s?house|Penthouse)"
        r"\s+in\s+(?P<location>[^,]+,\s*[^\d€]+?)\s*"
        r"€\s*(?P<price>[\d,]+)",
        re.UNICODE,
    )

    anchors = soup.find_all("a", href=re.compile(r"/en/property/\d+"))
    seen_urls = set()

    for a in anchors:
        href = a.get("href", "")
        if not href:
            continue
        clean_url = href.split("?")[0]
        if clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)

        text = a.get_text(separator=" ", strip=True)
        m = card_re.search(text)
        if not m:
            continue

        url = clean_url if clean_url.startswith("http") else f"https://www.kyero.com{clean_url}"

        # Agency name sits right before the trailing "* beds * baths * size"
        # stats block, e.g. "...parking Parking Golden Properties * 5 * 5 * 837 m²"
        agency = None
        agency_match = re.search(
            r"([A-Z][A-Za-z0-9&.'\- ]{2,50}?)\s*\*\s*\d+\s*\*\s*\d+\s*\*\s*[\d,]+\s*m²",
            text,
        )
        if agency_match:
            agency = agency_match.group(1).strip()

        results.append({
            "external_id": extract_external_id(url),
            "url": url,
            "title": m.group("type").strip(),
            "address": m.group("location").strip(),
            "agency": agency,
            "currency": "EUR",
            "price": safe_float(m.group("price")),
            "price_per_sqm": None,
        })

    return results





def parse_jamesedition(html: str):
    """
    Extract listings from a jamesedition.com search-results page. Confirmed
    structure (fetched live for both French Riviera and Geneva searches):
    each listing is a single <a href="/real_estate/{city-country}/{slug}-{id}">
    wrapping text like:

      "$12,587,180 7 Beds 2 Baths 3767 sqft Villa in Roquebrune-Cap-Martin,
       Provence-Alpes-Côte d'Azur, France Contact"

    or, when price is withheld:

      "Price On Request 4 Beds 3 Baths 3660 sqft Villa in Collonge-Bellerive,
       Genève, Switzerland Contact"

    IMPORTANT CURRENCY NOTE: JamesEdition shows USD by default (no EUR/CHF
    URL parameter confirmed). Prices here are stored as USD - don't compare
    them directly against EUR/CHF thresholds without accounting for that.
    See README.

    Returns a list of dicts: external_id, url, title, address, agency,
    currency ("USD"), price, price_per_sqm (always None - not shown)
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    card_re = re.compile(
        r"(?:\$(?P<price>[\d,]+)|Price On Request)\s*"
        r"(?:(?P<beds>\d+)\s*Beds?)?\s*(?:(?P<baths>\d+)\s*Baths?)?\s*"
        r"(?:[\d,]+\s*sqft)?\s*"
        r"(?P<type>House|Villa|Apartment|Estate|Penthouse|Castle|Chalet|Condo|Land)"
        r"\s+in\s+(?P<location>[^,]+,\s*[^,]+,\s*[^\n]+?)\s+Contact",
        re.UNICODE,
    )

    anchors = soup.find_all("a", href=re.compile(r"/real_estate/[a-z0-9-]+-\d+$"))
    seen_urls = set()

    for a in anchors:
        href = a.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        text = a.get_text(separator=" ", strip=True)
        m = card_re.search(text)
        if not m:
            continue

        url = href if href.startswith("http") else f"https://www.jamesedition.com{href}"

        # Agency/office name (e.g. "BARNES Valbonne", "Côte d'Azur Sotheby's
        # International Realty") sits as a sibling image-link right after
        # the listing anchor, followed by the individual agent's name link.
        # Walk forward through siblings and take the first substantial text
        # found - usually the office name; falls back to the agent's own
        # name if no office link is present.
        agency = None
        node = a.next_sibling
        hops = 0
        while node is not None and hops < 6:
            hops += 1
            candidate = node.get_text(strip=True) if hasattr(node, "get_text") \
                else str(node).strip()
            if candidate and "€" not in candidate and "$" not in candidate \
                    and len(candidate) < 80:
                agency = candidate
                break
            node = node.next_sibling

        results.append({
            "external_id": extract_external_id(url),
            "url": url,
            "title": m.group("type"),
            "address": m.group("location").strip(),
            "agency": agency,
            "currency": "USD",
            "price": safe_float(m.group("price")) if m.group("price") else None,
            "price_per_sqm": None,
        })

    return results





def parse_bellespierres(html: str):
    """
    Extract listings from a bellespierres.com search-results page.

    Structure here is spread out (unlike bellesdemeures/homegate/kyero
    where one anchor wraps everything): [photo gallery for listing N] ->
    "€X,XXX,XXX" -> "## Title   Location" -> "Xm² Y beds Z bath" -> a final
    empty anchor to the listing (its `title` attribute repeats type/beds/
    surface) -> "AGENCY NAME" -> [photo gallery for listing N+1] -> ...

    Strategy: every photo in a listing's gallery links to the same URL with
    the same `title` attribute, so naively deduping by "first occurrence"
    anchors at the position instead of the *last* occurrence, which put
    each listing's price-extraction window one listing too early. Fixed by
    keying on the LAST occurrence of each property ID, so the HTML slice
    between one listing's last anchor and the next listing's last anchor
    correctly contains that next listing's own price/title/dims.

    Agency name sits just after a listing's last anchor and before the next
    listing's photos, so it's read from a window immediately following
    that anchor, stopping before the next "€" price marker.
    """
    results = []

    anchor_re = re.compile(
        r'<a[^>]+href="([^"]*exceptionnal-property-(\d+)[^"]*)"[^>]*title="([^"]*)"'
    )
    matches = list(anchor_re.finditer(html))

    last_pos = {}  # pid -> (href, title_attr, start, end)
    order = []
    for m in matches:
        href, pid, title_attr = m.group(1), m.group(2), m.group(3)
        if pid not in last_pos:
            order.append(pid)
        last_pos[pid] = (href, title_attr, m.start(), m.end())

    for i, pid in enumerate(order):
        href, title_attr, start, end = last_pos[pid]
        chunk_start = last_pos[order[i - 1]][3] if i > 0 else max(0, start - 3000)
        chunk_html = html[chunk_start:start]
        chunk_text = BeautifulSoup(chunk_html, "html.parser").get_text(" ", strip=True)

        price_match = re.search(r'€\s*([\d,]+)', chunk_text)
        price = safe_float(price_match.group(1)) if price_match else None

        # Agency name: first line of text right after this listing's final
        # anchor, before the next listing's price ("€") appears
        agency = None
        window_html = html[end:end + 4000]
        window_text = BeautifulSoup(window_html, "html.parser").get_text("\n", strip=True)
        before_next_price = window_text.split("€")[0].strip()
        lines = [l.strip() for l in before_next_price.split("\n") if l.strip()]
        if lines and len(lines[0]) < 80:
            agency = lines[0]

        url = href if href.startswith("http") else f"https://www.bellespierres.com{href}"

        results.append({
            "external_id": pid,
            "url": url,
            "title": title_attr,   # e.g. "Villa with Sea view Cannes - 5 bedrooms - 270m²"
            "address": None,       # not reliably separable from title yet
            "agency": agency,
            "currency": "EUR",
            "price": price,
            "price_per_sqm": None,
        })

    return results





PARSERS = {
    "realadvisor": parse_realadvisor,
    "bellesdemeures": parse_bellesdemeures,
    "homegate": parse_homegate,
    "kyero": parse_kyero,
    "jamesedition": parse_jamesedition,
    "bellespierres": parse_bellespierres,
}


def extract_external_id(url: str) -> str:
    """Pull the trailing slug/ID out of a RealAdvisor listing URL."""
    return url.rstrip("/").split("/")[-1]


def safe_float(value):
    try:
        return float(str(value).replace("'", "").replace(",", ""))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Supabase I/O
# ---------------------------------------------------------------------------

def get_watchlist(only_id=None):
    url = f"{SUPABASE_URL}/rest/v1/pt_watchlist?active=eq.true"
    if only_id:
        url = f"{SUPABASE_URL}/rest/v1/pt_watchlist?id=eq.{only_id}"
    r = requests.get(url, headers=HEADERS_SUPABASE, timeout=30)
    r.raise_for_status()
    return r.json()


def upsert_listing(watchlist_id, source, item):
    """
    Insert or update a listing. Returns (listing_id, previous_price_or_None, is_new).
    """
    q = (f"{SUPABASE_URL}/rest/v1/pt_listings"
         f"?source=eq.{source}&external_id=eq.{item['external_id']}")
    r = requests.get(q, headers=HEADERS_SUPABASE, timeout=30)
    r.raise_for_status()
    existing = r.json()

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        row = existing[0]
        listing_id = row["id"]
        previous_price = row.get("current_price")

        patch = {
            "last_seen": now,
            "last_checked": now,
            "current_price": item["price"],
            "price_per_sqm": item["price_per_sqm"],
            "title": item["title"],
            "address": item["address"] or row.get("address"),
            "agency": item["agency"] or row.get("agency"),
            "is_delisted": False,
        }

        if previous_price is not None and item["price"] is not None and item["price"] < previous_price:
            drop = previous_price - item["price"]
            patch["price_change_amount"] = drop
            patch["price_change_pct"] = round(100 * drop / previous_price, 1)
            patch["price_dropped_at"] = now
        # Note: if the price goes back up or stays flat, we deliberately leave
        # price_change_amount as-is rather than clearing it, so a drop stays
        # visible on the dashboard until you've actually seen it. Clear it
        # manually in Supabase (set to null) once acted on, if you want that.

        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/pt_listings?id=eq.{listing_id}",
            headers=HEADERS_SUPABASE, json=patch, timeout=30,
        )
        r.raise_for_status()
        return listing_id, previous_price, False
    else:
        payload = {
            "watchlist_id": watchlist_id,
            "external_id": item["external_id"],
            "source": source,
            "url": item["url"],
            "title": item["title"],
            "address": item["address"],
            "agency": item["agency"],
            "currency": item["currency"],
            "current_price": item["price"],
            "price_per_sqm": item["price_per_sqm"],
        }
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/pt_listings",
            headers={**HEADERS_SUPABASE, "Prefer": "return=representation"},
            json=payload, timeout=30,
        )
        r.raise_for_status()
        listing_id = r.json()[0]["id"]
        return listing_id, None, True


SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def notify_slack(item, previous_price):
    if not SLACK_WEBHOOK_URL:
        return
    drop = previous_price - item["price"]
    pct = round(100 * drop / previous_price, 1)
    text = (
        f":small_red_triangle_down: *Price drop* — {item['title'] or 'Property'} "
        f"({item['address'] or 'unknown location'})\n"
        f"{previous_price:,.0f} -> *{item['price']:,.0f} EUR* (-{pct}%)\n"
        f"<{item['url']}|View listing>"
    )
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"  (Slack notification failed: {e})")


def log_price_history(listing_id, price):
    if price is None:
        return
    requests.post(
        f"{SUPABASE_URL}/rest/v1/pt_price_history",
        headers=HEADERS_SUPABASE,
        json={"listing_id": listing_id, "price": price},
        timeout=30,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch_page(url):
    r = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
    r.raise_for_status()
    return r.text


def run(diagnose_id=None):
    if not diagnose_id and (not SUPABASE_URL or not SUPABASE_KEY):
        sys.exit("Set SUPABASE_URL and SUPABASE_KEY env vars first.")

    if diagnose_id and not SUPABASE_URL:
        sys.exit("Diagnostic mode still needs SUPABASE_URL/KEY to read the watchlist row.")

    watchlist = get_watchlist(only_id=diagnose_id)
    drops_over_threshold = []

    for entry in watchlist:
        source = entry["source"]
        parser = PARSERS.get(source)
        if parser is None:
            print(f"Skipping '{entry['label']}' - no parser for source '{source}'")
            continue

        min_price = entry.get("min_price_eur") or 0
        max_pages = entry.get("max_pages") or 1

        print(f"\n=== {entry['label']} ({source}, min {min_price:,.0f}) ===")

        all_listings = []
        for page in range(1, max_pages + 1):
            page_url = paginate_url(entry["search_url"], page, source)
            try:
                html = fetch_page(page_url)
            except requests.HTTPError as e:
                print(f"  page {page}: request failed ({e}), stopping pagination for this entry")
                break
            listings = parser(html)
            if not listings:
                print(f"  page {page}: 0 listings parsed, stopping pagination")
                break
            print(f"  page {page}: {len(listings)} listings parsed")
            all_listings.extend(listings)
            time.sleep(REQUEST_DELAY_SECONDS)

        # Apply the price floor
        qualifying = [l for l in all_listings if l["price"] is not None and l["price"] >= min_price]
        print(f"Total: {len(all_listings)} parsed, {len(qualifying)} at/above {min_price:,.0f}")

        if diagnose_id:
            for item in qualifying[:15]:
                print(json.dumps(item, indent=2, ensure_ascii=False))
            continue  # don't write to DB in diagnostic mode

        for item in qualifying:
            listing_id, previous_price, is_new = upsert_listing(entry["id"], source, item)
            log_price_history(listing_id, item["price"])
            if not is_new and previous_price is not None and item["price"] < previous_price:
                drop = previous_price - item["price"]
                pct = round(100 * drop / previous_price, 1)
                drops_over_threshold.append(item)
                print(f"  PRICE DROP: {item['title']} — {item['address']} — "
                      f"{previous_price:,.0f} -> {item['price']:,.0f} ({pct}%) — {item['url']}")
                notify_slack(item, previous_price)

    if not diagnose_id:
        print(f"\n{len(drops_over_threshold)} price drop(s) found across all watchlist entries "
              f"(only counting listings at/above each entry's min_price_eur).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", type=int, help="watchlist id to test-run without writing to DB")
    args = parser.parse_args()
    run(diagnose_id=args.diagnose)
