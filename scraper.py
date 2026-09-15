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

REQUEST_DELAY_SECONDS = 3  # be polite - one request every few seconds, no concurrency

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

    # Skip test/throwaway watchlist entries that were never meant to run in
    # production. id 31 ("Geneve villas 5M+ (LuxuryEstate) - TEST") was left
    # active by mistake and was generating real alerts on the dashboard.
    # Also skip anything with "TEST" in the label going forward, so a future
    # test entry doesn't repeat the same problem silently. Note this filter
    # only applies to real (non-diagnostic) runs - `--diagnose 31` still
    # works if you ever want to manually check this entry.
    EXCLUDED_WATCHLIST_IDS = {31}
    if not diagnose_id:
        watchlist = [
            w for w in watchlist
            if w["id"] not in EXCLUDED_WATCHLIST_IDS and "test" not in w["label"].lower()
        ]

    alerts_raised = []
    # Page-1 failures are collected per source and reported in a single
    # Slack message at the end of the run, instead of one message per
    # watchlist entry. Crashes are collected the same way.
    failures_by_source = {}
    entries_per_source = {}
    run_errors = []

    for entry in watchlist:
        entries_per_source[entry["source"]] = entries_per_source.get(entry["source"], 0) + 1

    for entry in watchlist:
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
            for page in range(1, max_pages + 1):
                page_url = paginate_url(entry["search_url"], page, source)
                status_code, html, error = fetch_with_retry(page_url)

                if html is None:
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
                alert_state = get_listing_alert_state(listing_id)
                alert = compute_alert(alert_state.get("first_seen"), get_price_history(listing_id))
                if apply_alert(listing_id, alert_state, alert, item):
                    alerts_raised.append(item)

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
        print(f"\n{len(alerts_raised)} new/escalated alert(s) raised across all watchlist entries.")
        if run_errors:
            print(f"{len(run_errors)} watchlist entr"
                  f"{'y' if len(run_errors) == 1 else 'ies'} crashed - see tracebacks above.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", type=int, help="watchlist id to test-run without writing to DB")
    args = parser.parse_args()
    run(diagnose_id=args.diagnose)
