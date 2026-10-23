#!/usr/bin/env python3
"""
PrimeAuctions Price Tracker — RealAdvisor + LuxuryEstate scraper
----------------------------------------------------------------
Fetches search-result pages from RealAdvisor.ch and LuxuryEstate.com,
extracts listings (price, address, agency, url), and logs them to Supabase
so price drops over time become visible across the four PrimeAuctions
regions (Cote d'Azur, Alpes, Geneve, Costa del Sol).

STATUS AS OF SEPTEMBER 2026: these two are the only sources that work
with a plain script. bellesdemeures and bien'ici need JavaScript
rendering (plain requests sees an empty shell); BellesPierres,
JamesEdition and homegate return 403 Forbidden even from a residential
IP. Their parsers were deleted on 15 Sep 2026 rather than left in place
pretending to be options; git history has them if a source reopens.

One hard-won note on headers: do NOT add "br" to Accept-Encoding below.
Chrome advertises Brotli, but the requests library cannot decompress it
unless the brotli package is installed, and this workflow installs only
requests + beautifulsoup4. RealAdvisor honours br, so advertising it
returns 100KB of binary garbage that parses to zero listings while
looking like a healthy HTTP 200. That cost an afternoon.

IMPORTANT — read before relying on this:
Each source's parser was written from a manual inspection of that site's
rendered output, not from continuous live testing. Run new or changed
watchlist entries in DIAGNOSTIC MODE first (see bottom of file) and
eyeball a few parsed listings against the real page before trusting them
or scheduling a real run.

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
import random
import argparse
import traceback
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

# A real Chrome sends far more than a User-Agent. A request carrying only
# UA + Accept-Language is trivially fingerprinted as a script by DataDome /
# Cloudflare, which is how BellesPierres, JamesEdition and homegate were
# lost. These are the headers Chrome actually sends on a top-level
# navigation, in Chrome's order.
HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# Pauses are randomised so the traffic doesn't have a machine-regular rhythm:
# a few seconds between pages, a longer break between two zones.
REQUEST_DELAY_RANGE = (4, 9)     # seconds between two pages
ENTRY_PAUSE_RANGE = (15, 45)     # seconds between two watchlist entries

# Rotation: each watchlist entry is scraped one day out of N, so the daily
# load on the sources is divided by N. Luxury listings rarely reprice, so a
# drop seen up to N days late changes nothing for the business. A manual run
# with FULL_RUN=1 scrapes every entry.
SCRAPE_EVERY_N_DAYS = int(os.environ.get("SCRAPE_EVERY_N_DAYS", "3") or 3)
FULL_RUN = os.environ.get("FULL_RUN", "").strip().lower() in ("1", "true", "yes")

# Transient failures (a block, a 5xx, a dropped connection) used to kill an
# entry for the whole day and fire a Slack alert. Retry a couple of times
# with backoff before believing the source is really down.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [8, 25]  # wait before attempt 2, then before attempt 3
RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


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

        title, address, agency = split_realadvisor_card(text, url)

        if price is None and "Prix sur demande" not in text:
            continue  # couldn't parse a usable price, skip rather than store garbage

        results.append({
            "external_id": external_id,
            "url": url,
            "title": title,
            "address": address,
            "agency": agency,
            "currency": "CHF",
            "price": price,
            "price_per_sqm": price_per_sqm,
        })

    return results


# RealAdvisor listing types, longest first so "Maison individuelle" is
# matched before the bare "Maison".
REALADVISOR_TYPES = (
    "Maison individuelle", "Maison mitoyenne", "Maison villageoise",
    "Appartement en attique", "Appartement", "Attique", "Duplex", "Triplex",
    "Villa", "Chalet", "Loft", "Studio", "Propriété", "Immeuble", "Terrain",
    "Maison",
)


def split_realadvisor_card(text: str, url: str):
    """
    Turn one RealAdvisor card's run-on text into (title, address, agency).

    A card reads, in order:
        "il y a 6 heures  Attica Immobilier  5 pièces • 150 m² •
         1185 m² Terrain • Maison individuelle  1227 Carouge GE  Espaces lumi..."
    i.e. [time posted] [agency] [rooms/surface] [type] [postal code + town]
    [start of the description]. Storing all of that truncated at 120 chars
    as the "title" made the dashboard unreadable and left address and
    agency permanently null.

    Every field degrades to None rather than to a wrong guess: a partial
    record with a correct price is useful, a confident wrong address is not.
    """
    body = re.sub(r"^(il y a\s+\d+\s+\S+|Nouveau(?:té)?)\s*", "", text or "").strip()

    # Agency: sits between the time prefix and the room count, e.g.
    # "Attica Immobilier 5 pièces". Requires a capitalised start, so a card
    # with no agency line (which begins "5 pièces") yields None.
    agency = None
    m = re.match(r"([A-ZÀ-Ü][^•]{1,60}?)\s+\d+(?:[.,]\d+)?\s*pi[eè]ces", body)
    if m:
        agency = m.group(1).strip()

    prop_type = next((t for t in REALADVISOR_TYPES if t.lower() in body.lower()), None)
    rooms = re.search(r"(\d+(?:[.,]\d+)?)\s*pi[eè]ces", body)
    surface = re.search(r"(\d[\d'\s]{0,8})\s*m²", body)

    bits = [prop_type] if prop_type else []
    if rooms:
        bits.append(f"{rooms.group(1)} pièces")
    if surface:
        bits.append(f"{surface.group(1).strip()} m²")
    title = ", ".join(bits) if bits else (body[:80].strip() or None)

    # Address from the URL slug, which is far more reliable than the card
    # text: /fr/acheter/maison/1227-carouge-ge-OOG4-LQ7X -> "1227 Carouge GE".
    # The trailing segments are RealAdvisor's own id and always contain
    # uppercase, so keep leading segments while they are lowercase or digits.
    address = None
    slug = (url or "").rstrip("/").split("/")[-1]
    parts = slug.split("-")
    keep = []
    for part in parts:
        if part.isdigit() or part.islower():
            keep.append(part)
        else:
            break
    if len(keep) >= 2 and keep[0].isdigit():
        address = " ".join(
            w.upper() if len(w) == 2 else w.capitalize() for w in keep
        )
        address = address.replace(keep[0].capitalize(), keep[0], 1)

    return title, address, agency


def paginate_url(base_url: str, page: int, source: str) -> str:
    """The two live sources paginate differently."""
    if page <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    if source == "realadvisor":
        return f"{base_url}{sep}page={page}"
    if source == "luxuryestate":
        return f"{base_url}{sep}pag={page}"
    return base_url


def parse_luxuryestate(html: str):
    """
    Extract listings from a luxuryestate.com search-results page (confirmed
    reachable with a plain script — no JS-rendering, no 403 block, unlike
    bellesdemeures/bien'ici and BellesPierres/JamesEdition/homegate).

    Structure (inferred from one manual fetch, NOT yet eyeballed against
    live diagnose output — treat this parser as unverified until you've
    run --diagnose on a real watchlist entry and checked a few results):
    each listing's title anchor has the form
        <a href="https://www.luxuryestate.com/p{ID}-{type}-for-sale-{city}">
            Villa in Nice, Alpes-Maritimes
        </a>
    followed (within the same card, before the next listing's title anchor)
    by a price line "€ 6,800,000", a size/rooms line "278 m² 3 4", a
    description paragraph, and an agency credit line "Presented by
    {agency name}".

    Returns a list of dicts: external_id, url, title, address, agency,
    currency ("EUR"), price, price_per_sqm (always None - not shown)
    """
    results = []

    anchor_re = re.compile(
        r'<a[^>]+href="(https://www\.luxuryestate\.com/p(\d+)-[a-z0-9-]+)"[^>]*>'
        r'\s*([A-Za-z][A-Za-z \'-]*?)\s+in\s+([^<]+?)\s*</a>',
        re.UNICODE,
    )
    matches = list(anchor_re.finditer(html))

    for i, m in enumerate(matches):
        url, pid, prop_type, location = m.group(1), m.group(2), m.group(3), m.group(4)

        chunk_end = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 3000
        chunk_html = html[m.end():chunk_end]
        chunk_text = BeautifulSoup(chunk_html, "html.parser").get_text(" ", strip=True)

        price_match = re.search(r'€\s*([\d,]+)', chunk_text)
        price = safe_float(price_match.group(1)) if price_match else None

        sqm_match = re.search(r'([\d,]+)\s*m[²2]', chunk_text)
        surface = safe_float(sqm_match.group(1)) if sqm_match else None
        price_per_sqm = round(price / surface, 0) if price and surface else None

        agency = None
        agency_match = re.search(r'Presented by\s+([^<\n]{2,80}?)(?:\s*Contact|\s*Elite|\s*Prestige|\s*Premium|$)', chunk_text)
        if agency_match:
            agency = agency_match.group(1).strip()

        results.append({
            "external_id": pid,
            "url": url,
            "title": prop_type.strip(),
            "address": location.strip(),
            "agency": agency,
            "currency": "EUR",
            "price": price,
            "price_per_sqm": price_per_sqm,
        })

    return results


# Only the sources that actually work with a plain script. The parsers for
# bellesdemeures, homegate, kyero, jamesedition and bellespierres were
# removed on 15 Sep 2026: those sites either block a scripted request or
# need JavaScript rendering, so their parsers were ~350 lines of code that
# could never run. They remain in git history if a source ever reopens.
PARSERS = {
    "realadvisor": parse_realadvisor,
    "luxuryestate": parse_luxuryestate,
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
            "delisted_at": None,
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


def explain_failure(status_code, html, error):
    """
    Turn a failed page-1 fetch into an honest one-line diagnosis.

    The old version scanned the page body for block keywords even when the
    fetch had raised - in which case the body was always the empty string,
    so every HTTP error reported "no block keywords found - page structure
    may have changed". For a 403 that is exactly backwards: the structure
    is fine, we were blocked. Lead with the status code, which is the
    strongest evidence available.
    """
    if status_code in (403, 429):
        return ("blocked by the site (the request signature or the runner IP is "
                "being rejected) - this is not a parser problem")
    if status_code and 500 <= status_code < 600:
        return "the site returned a server error - usually temporary"
    if not status_code:
        return f"could not connect ({error or 'network error'})"
    if status_code == 200:
        lowered = (html or "").lower()
        found = [s for s in BLOCK_SIGNALS if s in lowered]
        if found:
            return f"page loaded but shows block signal(s): {found}"
        return ("page loaded normally but 0 listings parsed - the page structure "
                "has probably changed")
    return f"unexpected HTTP {status_code}"


def notify_slack_source_failures(source, failures, total_for_source):
    """
    ONE message per source per run, not one per watchlist entry.

    Ten watchlist entries share the luxuryestate source, so a single block
    used to produce ten identical Slack messages 90 seconds apart. Grouping
    by source makes the real shape of the incident visible at a glance:
    "10 of 10 entries failed" is a dead source, "1 of 10" is a blip.
    """
    if not SLACK_WEBHOOK_URL or not failures:
        return
    statuses = sorted({f["status"] for f in failures})
    status_text = ", ".join(f"HTTP {s}" if s else "no response" for s in statuses)
    reason = explain_failure(failures[0]["status"], failures[0]["html"], failures[0]["error"])

    if len(failures) == total_for_source:
        headline = f":warning: *Source may be down* — {source} ({status_text})"
        detail = ("Its only watchlist entry failed on page 1." if total_for_source == 1
                  else f"All {total_for_source} watchlist entries failed on page 1.")
    else:
        headline = f":warning: *Partial failure* — {source} ({status_text})"
        detail = f"{len(failures)} of {total_for_source} watchlist entries failed on page 1."

    affected = "\n".join(
        f"• {f['label']} (id {f['id']})" for f in failures[:10]
    )
    if len(failures) > 10:
        affected += f"\n• ...and {len(failures) - 10} more"

    text = (
        f"{headline}\n{detail} Retried {FETCH_ATTEMPTS}x with backoff.\n"
        f"Likely cause: {reason}.\n{affected}\n"
        f"Run `python scraper.py --diagnose {failures[0]['id']}` to inspect."
    )
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"  (Slack notification failed: {e})")


def notify_slack_run_errors(errors):
    """One message listing entries that crashed outright, so a bug in one
    entry is visible instead of silently taking the whole run down."""
    if not SLACK_WEBHOOK_URL or not errors:
        return
    lines = "\n".join(f"• {label} (id {eid}): {msg}" for eid, label, msg in errors[:10])
    text = (
        f":rotating_light: *Scraper error* — {len(errors)} watchlist entr"
        f"{'y' if len(errors) == 1 else 'ies'} crashed during the run\n{lines}\n"
        f"The rest of the run completed; see the GitHub Actions log for tracebacks."
    )
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"  (Slack notification failed: {e})")


def mark_delisted(watchlist_id, seen_external_ids, run_started_at):
    """
    Called only after a COMPLETE scan of a zone (pagination reached the real
    end of the results). Listings of that zone that were not on any page this
    time have left the portal: sold, withdrawn or expired. Zones capped by
    max_pages never call this, because a listing beyond the cap would be
    wrongly reported as removed. Listings under the price floor are still in
    seen_external_ids, so a price cut below the floor is not a removal.
    """
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pt_listings?watchlist_id=eq.{watchlist_id}"
        f"&is_delisted=eq.false&last_seen=lt.{run_started_at}&select=id,external_id",
        headers=HEADERS_SUPABASE, timeout=30,
    )
    r.raise_for_status()
    gone = [row["id"] for row in r.json() if row["external_id"] not in seen_external_ids]
    if not gone:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    for i in range(0, len(gone), 100):
        ids = ",".join(str(x) for x in gone[i:i + 100])
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/pt_listings?id=in.({ids})",
            headers=HEADERS_SUPABASE, json={"is_delisted": True, "delisted_at": now}, timeout=30,
        ).raise_for_status()
    return len(gone)


def record_scan(watchlist_id, complete, pages_read):
    now = datetime.now(timezone.utc).isoformat()
    patch = {"last_scan_at": now, "last_scan_complete": complete, "last_scan_pages": pages_read}
    if complete:
        patch["last_complete_scan_at"] = now
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/pt_watchlist?id=eq.{watchlist_id}",
            headers=HEADERS_SUPABASE, json=patch, timeout=30,
        ).raise_for_status()
    except requests.RequestException as e:
        print(f"  (could not record scan status: {e})")


VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:brassat@primeauctionsglobal.com")


def count_open_alerts():
    """Open (not dismissed) orange/red alerts, counted per property: the same
    villa listed by several agencies counts once, as on the dashboard."""
    wl = requests.get(f"{SUPABASE_URL}/rest/v1/pt_watchlist?active=eq.true&select=id",
                      headers=HEADERS_SUPABASE, timeout=30)
    wl.raise_for_status()
    active = {w["id"] for w in wl.json()}
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pt_listings?alert_tier=in.(orange,red)"
        f"&select=watchlist_id,current_price,price_per_sqm,alert_tier,dismissed_at,dismissed_tier",
        headers=HEADERS_SUPABASE, timeout=30)
    r.raise_for_status()
    keys = set()
    for l in r.json():
        if l["watchlist_id"] not in active:
            continue
        if l.get("dismissed_at") and TIER_RANK.get(l.get("dismissed_tier"), 0) >= TIER_RANK.get(l["alert_tier"], 0):
            continue
        area = round(l["current_price"] / l["price_per_sqm"]) if l.get("price_per_sqm") and l.get("current_price") else None
        keys.add((l["current_price"], area))
    return len(keys)


def send_alert_push(alerts_raised):
    """
    Push notification to every phone that pressed "Alert me" in the app. The
    service worker shows the notification and sets the red badge on the app
    icon to the number of open alerts. Only new or escalated alerts trigger
    it, never ordinary activity.
    """
    if not VAPID_PRIVATE_KEY:
        print("  (push skipped: VAPID_PRIVATE_KEY not set)")
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("  (push skipped: pywebpush not installed)")
        return
    r = requests.get(f"{SUPABASE_URL}/rest/v1/pt_push_subscriptions?select=id,subscription",
                     headers=HEADERS_SUPABASE, timeout=30)
    r.raise_for_status()
    subs = r.json()
    if not subs:
        return
    first = alerts_raised[0]
    body = f"{first.get('title') or 'Property'}, {first.get('address') or ''}: {_fmt_eur(first['price'])} \u20ac"
    if len(alerts_raised) > 1:
        body += f" (+{len(alerts_raised) - 1} more)"
    payload = json.dumps({
        "title": "New alert" if len(alerts_raised) == 1 else f"{len(alerts_raised)} new alerts",
        "body": body,
        "badge": count_open_alerts(),
    })
    for s in subs:
        try:
            webpush(subscription_info=s["subscription"], data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT}, ttl=86400)
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):  # phone unsubscribed or app removed: forget it
                requests.delete(f"{SUPABASE_URL}/rest/v1/pt_push_subscriptions?id=eq.{s['id']}",
                                headers=HEADERS_SUPABASE, timeout=30)
            else:
                print(f"  (push failed: {e})")
    print(f"  push sent to {len(subs)} device(s)")


def watchlist_has_listings(watchlist_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pt_listings?watchlist_id=eq.{watchlist_id}&select=id&limit=1",
        headers=HEADERS_SUPABASE, timeout=30,
    )
    r.raise_for_status()
    return bool(r.json())


def _fmt_eur(v):
    return f"{v:,.0f}".replace(",", " ")


def notify_daily_digest(digest, alerts_raised):
    """
    One Slack message per run, every run, even when nothing happened.
    It lists every price drop (however small), so the tracker visibly lives
    day to day without touching the alert thresholds in compute_alert. New
    listings are deliberately left out: they are not an auction signal. The
    first line doubles as a heartbeat: no digest in Slack means the run did
    not complete.
    """
    if not SLACK_WEBHOOK_URL:
        return
    day = datetime.now(timezone.utc).strftime("%d %b")
    lines = [f":bar_chart: *Price Tracker, {day}*: {len(digest['zones'])} zone(s) scanned today"]
    if digest["zones"]:
        lines.append("_" + ", ".join(sorted(digest["zones"])) + "_")

    # The same property is often listed by several agencies: one line per
    # property (same old price, same new price, same surface).
    unique = {}
    for item, prev in digest["drops"]:
        area = round(item["price"] / item["price_per_sqm"]) if item.get("price_per_sqm") else item.get("title")
        unique.setdefault((prev, item["price"], area), (item, prev))
    drops = sorted(unique.values(), key=lambda d: (d[1] - d[0]["price"]) / d[1], reverse=True)
    if drops:
        lines.append(f"\n*Price drops ({len(drops)})*")
        for item, prev in drops[:15]:
            pct = round(100 * (prev - item["price"]) / prev, 1)
            lines.append(f"• -{pct}% {item['title'] or 'Property'} ({item['address'] or '?'}): "
                         f"{_fmt_eur(prev)} \u2192 {_fmt_eur(item['price'])} <{item['url']}|view>")
        if len(drops) > 15:
            lines.append(f"• ...and {len(drops) - 15} more on the dashboard")
    else:
        lines.append("\nNo price drop today.")

    for label, n in digest["baselines"]:
        lines.append(f"\n:new: Zone added: *{label}*, {n} listings now tracked")

    if digest["removed"]:
        lines.append(f"\n{digest['removed']} listing(s) no longer on the portal (sold, withdrawn or expired).")

    if alerts_raised:
        lines.append(f"\n{len(alerts_raised)} new or escalated alert(s), see the messages above.")

    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": "\n".join(lines)}, timeout=15)
    except requests.RequestException as e:
        print(f"  (Slack digest failed: {e})")


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
# Market-anomaly detection (Phase 1)
# ---------------------------------------------------------------------------
#
# Reframes the product from "here's every price drop" to "here's whether
# today's data suggests a genuine auction opportunity." Three signals, all
# derived from data already being collected (no new scraping required):
#   - days on market (first_seen -> now)
#   - number of actual price REDUCTIONS across a listing's whole history
#     (not just the latest check-to-check delta)
#   - cumulative % drop from the first ever recorded price to the latest
#
# Deliberately a simple, inspectable point score (not a black-box
# confidence percentage) - there's no outcome data yet to calibrate a real
# probability against, and a fake-precise number would erode trust faster
# than an honest, adjustable rule would.

TIER_RANK = {None: 0, "green": 0, "orange": 1, "red": 2}
TIER_EMOJI = {"orange": ":large_orange_circle:", "red": ":red_circle:"}


def get_price_history(listing_id):
    """All recorded prices for a listing, oldest first."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pt_price_history"
        f"?listing_id=eq.{listing_id}&select=price,checked_at&order=checked_at.asc",
        headers=HEADERS_SUPABASE, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_listing_alert_state(listing_id):
    """Current alert bookkeeping for a listing, so we know whether this
    run's computed tier is new/escalated, or already known/dismissed."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pt_listings"
        f"?id=eq.{listing_id}&select=first_seen,alert_tier,dismissed_at,dismissed_tier",
        headers=HEADERS_SUPABASE, timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else {}


def compute_alert(first_seen_iso, history):
    """
    Score a listing's history against the three Phase 1 signals.

    Needs at least 2 price points to say anything meaningful - a listing
    seen only once hasn't had a chance to show any of these signals yet,
    so this correctly returns tier "green" (no alert) rather than guessing
    off a single data point.

    Returns {"tier": "green"|"orange"|"red", "score": int, "reasons": [str]}
    """
    prices = [h["price"] for h in history if h.get("price") is not None]
    if len(prices) < 2:
        return {"tier": "green", "score": 0, "reasons": []}

    first_price = prices[0]
    current_price = prices[-1]
    reduction_count = sum(1 for i in range(1, len(prices)) if prices[i] < prices[i - 1])
    cumulative_pct = round(100 * (first_price - current_price) / first_price, 1) \
        if first_price else 0

    days_on_market = None
    if first_seen_iso:
        try:
            first_seen_dt = datetime.fromisoformat(first_seen_iso.replace("Z", "+00:00"))
            days_on_market = (datetime.now(timezone.utc) - first_seen_dt).days
        except ValueError:
            pass

    score = 0
    reasons = []

    if days_on_market is not None:
        if days_on_market >= 365:
            score += 3
            reasons.append(f"{days_on_market} days on market")
        elif days_on_market >= 180:
            score += 1
            reasons.append(f"{days_on_market} days on market")

    if reduction_count >= 4:
        score += 3
        reasons.append(f"{reduction_count} price reductions")
    elif reduction_count >= 2:
        score += 1
        reasons.append(f"{reduction_count} price reductions")

    if cumulative_pct >= 15:
        score += 3
        reasons.append(f"Cumulative drop: {cumulative_pct}% ({first_price:,.0f} \u2192 {current_price:,.0f})")
    elif cumulative_pct >= 7:
        score += 1
        reasons.append(f"Cumulative drop: {cumulative_pct}% ({first_price:,.0f} \u2192 {current_price:,.0f})")

    if score >= 5:
        tier = "red"
    elif score >= 2:
        tier = "orange"
    else:
        tier = "green"

    return {"tier": tier, "score": score, "reasons": reasons}


def apply_alert(listing_id, alert_state, alert, item):
    """
    Writes the freshly-computed tier/score/reasons to pt_listings, clears
    a stale dismissal if the tier has escalated past what was dismissed
    (so a previously-handled listing resurfaces if it gets worse), and
    triggers exactly one Slack ping per new escalation - never repeats a
    ping for a tier that's already been notified about.

    Returns True if this run raised a NEW or ESCALATED orange/red alert
    (used only for the run's summary line).
    """
    old_tier = alert_state.get("alert_tier")
    dismissed_tier = alert_state.get("dismissed_tier")
    new_tier = alert["tier"]

    patch = {
        "alert_tier": new_tier,
        "alert_score": alert["score"],
        "alert_reasons": "\n".join(alert["reasons"]) if alert["reasons"] else None,
    }

    if alert_state.get("dismissed_at") and TIER_RANK.get(new_tier, 0) > TIER_RANK.get(dismissed_tier, 0):
        patch["dismissed_at"] = None
        patch["dismissed_tier"] = None

    requests.patch(
        f"{SUPABASE_URL}/rest/v1/pt_listings?id=eq.{listing_id}",
        headers=HEADERS_SUPABASE, json=patch, timeout=30,
    ).raise_for_status()

    escalated = new_tier in ("orange", "red") and TIER_RANK.get(new_tier, 0) > TIER_RANK.get(old_tier, 0)
    if escalated:
        print(f"  ALERT ({new_tier.upper()}): {item['title']} — {item['address']} — "
              f"{'; '.join(alert['reasons'])} — {item['url']}")
        notify_alert_slack(item, new_tier, alert["reasons"])

    return escalated


def notify_alert_slack(item, tier, reasons):
    if not SLACK_WEBHOOK_URL:
        return
    emoji = TIER_EMOJI.get(tier, "")
    label = "Strong auction signal" if tier == "red" else "Worth reviewing"
    reason_lines = "\n".join(f"\u2022 {r}" for r in reasons)
    text = (
        f"{emoji} *{label}* — {item['title'] or 'Property'} "
        f"({item['address'] or 'unknown location'})\n"
        f"Reasons:\n{reason_lines}\n"
        f"<{item['url']}|View listing>"
    )
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"  (Slack notification failed: {e})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch_page(url):
    r = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_with_retry(url, attempts=FETCH_ATTEMPTS):
    """
    Fetch a page, retrying transient failures with jittered backoff.

    Returns (status_code, html, error): html is None unless the fetch
    succeeded, status_code is 0 when we never got a response at all.
    Connection errors and timeouts are handled here rather than thrown -
    previously only requests.HTTPError was caught at the call site, so a
    dropped connection on one page aborted the entire run and every
    remaining watchlist entry was silently skipped.
    """
    status_code, error = 0, None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            wait = RETRY_BACKOFF_SECONDS[min(attempt - 2, len(RETRY_BACKOFF_SECONDS) - 1)]
            wait += random.uniform(0, 3)  # jitter, so 10 entries don't retry in lockstep
            print(f"    attempt {attempt - 1} failed ({error}); retrying in {wait:.0f}s")
            time.sleep(wait)
        try:
            r = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
            status_code = r.status_code
            if r.status_code == 200:
                return status_code, r.text, None
            error = f"HTTP {r.status_code}"
            if r.status_code not in RETRYABLE_STATUS:
                break  # 404 and friends won't fix themselves
        except requests.RequestException as e:
            status_code, error = 0, type(e).__name__
    return status_code, None, error


BLOCK_SIGNALS = [
    "captcha", "are you a robot", "access denied", "attention required",
    "unusual traffic", "cloudflare", "datadome", "please verify",
    "enable javascript", "just a moment",
]


def diagnose_empty_page(html: str, status_code: int):
    """
    Called only when a page parsed to 0 listings in diagnose mode. Doesn't
    guess why - prints concrete evidence so a human can tell the difference
    between "site blocked us" and "page structure changed" at a glance.
    """
    print(f"    HTTP status: {status_code}")
    print(f"    Response size: {len(html):,} characters")

    lowered = html.lower()
    found_signals = [s for s in BLOCK_SIGNALS if s in lowered]
    if found_signals:
        print(f"    Possible bot-block signal(s) found in page text: {found_signals}")
    else:
        print("    No obvious bot-block keywords found - looks like a normal page, "
              "so the parser's selectors likely don't match this page's current structure.")

    # Print a short, human-readable snippet so you can eyeball what actually
    # came back, without dumping the whole page
    visible_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    print(f"    First 300 characters of visible page text:")
    print(f"    \"{visible_text[:300]}\"")


def run(diagnose_id=None):
    if not diagnose_id and (not SUPABASE_URL or not SUPABASE_KEY):
        sys.exit("Set SUPABASE_URL and SUPABASE_KEY env vars first.")

    if diagnose_id and not SUPABASE_URL:
        sys.exit("Diagnostic mode still needs SUPABASE_URL/KEY to read the watchlist row.")

    watchlist = get_watchlist(only_id=diagnose_id)

    # Skip test/throwaway watchlist entries: anything with "TEST" in its
    # label, plus any id listed below. id 31 (Geneva on LuxuryEstate) used to
    # be excluded here; since Oct 2026 it is Geneva's main source and
    # RealAdvisor (id 16) is deactivated. The filter only applies to real
    # runs, `--diagnose <id>` still works on any entry.
    EXCLUDED_WATCHLIST_IDS = set()
    if not diagnose_id:
        watchlist = [
            w for w in watchlist
            if w["id"] not in EXCLUDED_WATCHLIST_IDS and "test" not in w["label"].lower()
        ]

    # Rotation: today's share of the watchlist only (see SCRAPE_EVERY_N_DAYS).
    if not diagnose_id and not FULL_RUN and SCRAPE_EVERY_N_DAYS > 1:
        today_slot = datetime.now(timezone.utc).date().toordinal() % SCRAPE_EVERY_N_DAYS
        watchlist = [w for w in watchlist if w["id"] % SCRAPE_EVERY_N_DAYS == today_slot]
        print(f"Rotation 1/{SCRAPE_EVERY_N_DAYS} days: {len(watchlist)} entries today: "
              + ", ".join(w["label"] for w in watchlist))
    random.shuffle(watchlist)  # different order every day

    # Collected for the daily Slack digest
    digest = {"zones": [], "new": [], "drops": [], "baselines": [], "removed": 0}
    run_started_at = datetime.now(timezone.utc).isoformat()

    alerts_raised = []
    # Page-1 failures are collected per source and reported in a single
    # Slack message at the end of the run, instead of one message per
    # watchlist entry. Crashes are collected the same way.
    failures_by_source = {}
    entries_per_source = {}
    run_errors = []

    for entry in watchlist:
        entries_per_source[entry["source"]] = entries_per_source.get(entry["source"], 0) + 1

    for entry_index, entry in enumerate(watchlist):
        if entry_index > 0 and not diagnose_id:
            time.sleep(random.uniform(*ENTRY_PAUSE_RANGE))
        source = entry["source"]
        parser = PARSERS.get(source)
        if parser is None:
            print(f"Skipping '{entry['label']}' - no parser for source '{source}'")
            continue

        min_price = entry.get("min_price_eur") or 0
        max_pages = entry.get("max_pages") or 1

        print(f"\n=== {entry['label']} ({source}, min {min_price:,.0f}) ===")

        # One entry blowing up must never take the rest of the run with it.
        # A crashed run sends nothing at all, which looks exactly like a
        # quiet day in Slack - the worst possible failure mode for a
        # monitoring tool.
        try:
            all_listings = []
            seen_ids = set()
            complete = False   # True only if pagination reached the real end of the results
            pages_read = 0
            for page in range(1, max_pages + 1):
                page_url = paginate_url(entry["search_url"], page, source)
                status_code, html, error = fetch_with_retry(page_url)

                if html is None:
                    if page > 1 and status_code == 404:
                        complete = True  # past the last page of results
                        print(f"  page {page}: 404, end of results")
                        break
                    print(f"  page {page}: fetch failed after {FETCH_ATTEMPTS} attempts "
                          f"({error}), stopping pagination for this entry")
                    if not diagnose_id and page == 1:
                        failures_by_source.setdefault(source, []).append({
                            "id": entry["id"], "label": entry["label"],
                            "status": status_code, "html": "", "error": error,
                        })
                    elif diagnose_id and page == 1:
                        print(f"    {explain_failure(status_code, '', error)}")
                    break

                listings = parser(html)
                if listings and page > 1 and all(l["external_id"] in seen_ids for l in listings):
                    # Some sites serve the last page again past the end
                    complete = True
                    print(f"  page {page}: only repeats, end of results")
                    break
                if not listings and page > 1:
                    complete = True
                if not listings:
                    print(f"  page {page}: 0 listings parsed, stopping pagination")
                    if diagnose_id:
                        diagnose_empty_page(html, status_code)
                    elif page == 1:
                        # Page 1 empty on a real run is the strong "source is
                        # probably dead" signal - alert distinctly from price drops
                        failures_by_source.setdefault(source, []).append({
                            "id": entry["id"], "label": entry["label"],
                            "status": status_code, "html": html, "error": None,
                        })
                    break
                print(f"  page {page}: {len(listings)} listings parsed")
                pages_read = page
                all_listings.extend(listings)
                seen_ids.update(l["external_id"] for l in listings)
                time.sleep(random.uniform(*REQUEST_DELAY_RANGE))

            # Apply the price floor
            qualifying = [l for l in all_listings if l["price"] is not None and l["price"] >= min_price]
            print(f"Total: {len(all_listings)} parsed, {len(qualifying)} at/above {min_price:,.0f}")

            if diagnose_id:
                for item in qualifying[:15]:
                    print(json.dumps(item, indent=2, ensure_ascii=False))
                continue  # don't write to DB in diagnostic mode

            # A zone scraped for the first time would otherwise report every
            # listing as "new" in the digest. Report it once as a baseline.
            is_baseline = not watchlist_has_listings(entry["id"])
            digest["zones"].append(entry["label"])
            if is_baseline and qualifying:
                digest["baselines"].append((entry["label"], len(qualifying)))

            for item in qualifying:
                listing_id, previous_price, is_new = upsert_listing(entry["id"], source, item)
                if is_new and not is_baseline:
                    digest["new"].append(item)
                if (previous_price is not None and item["price"] is not None
                        and item["price"] < previous_price):
                    digest["drops"].append((item, previous_price))
                log_price_history(listing_id, item["price"])
                alert_state = get_listing_alert_state(listing_id)
                alert = compute_alert(alert_state.get("first_seen"), get_price_history(listing_id))
                if apply_alert(listing_id, alert_state, alert, item):
                    alerts_raised.append(item)

            if not complete and pages_read >= max_pages:
                print(f"  capped at {max_pages} pages: removals can't be detected for this zone")
            if complete:
                n_gone = mark_delisted(entry["id"], seen_ids, run_started_at)
                if n_gone:
                    print(f"  {n_gone} listing(s) no longer on the portal, marked as removed")
                    digest["removed"] += n_gone
            record_scan(entry["id"], complete, pages_read)

        except Exception as e:  # noqa: BLE001 - deliberate catch-all, see comment above
            print(f"  ERROR on '{entry['label']}' (id {entry['id']}): {e!r}")
            traceback.print_exc()
            run_errors.append((entry["id"], entry["label"], f"{type(e).__name__}: {e}"))
            continue

    if not diagnose_id:
        for source, failures in failures_by_source.items():
            print(f"\nALERT: {len(failures)}/{entries_per_source.get(source, len(failures))} "
                  f"entries failed for source '{source}' - notifying Slack")
            notify_slack_source_failures(source, failures, entries_per_source.get(source, len(failures)))
        notify_slack_run_errors(run_errors)
        notify_daily_digest(digest, alerts_raised)
        if alerts_raised:
            send_alert_push(alerts_raised)
        print(f"\n{len(alerts_raised)} new/escalated alert(s) raised across all watchlist entries.")
        if run_errors:
            print(f"{len(run_errors)} watchlist entr"
                  f"{'y' if len(run_errors) == 1 else 'ies'} crashed - see tracebacks above.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", type=int, help="watchlist id to test-run without writing to DB")
    args = parser.parse_args()
    run(diagnose_id=args.diagnose)
