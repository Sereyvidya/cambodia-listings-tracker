"""
Heuristic extraction of listing fields from free-text Telegram messages.

This is regex/keyword based, not ML. Cambodian real estate posts are
pretty formulaic (price, location, bed/bath count, contact number all
tend to appear somewhere in the text) so this gets you most of the way
there, but it WILL misparse or miss things on unusual phrasing. Treat
the extracted fields as "best guess" filters for the dashboard, and
raw_text as the source of truth.

Tune the constants below (LOCATIONS, PROPERTY_TYPES, KHR_PER_USD) as
you see what actually comes through your groups.
"""

import re
import hashlib

# Approximate exchange rate for normalizing riel prices to USD.
# Cambodia is heavily USD-dollarized so most real listings are already
# quoted in USD; this only kicks in for the minority quoted in riel.
# Update this occasionally -- it does NOT update itself.
KHR_PER_USD = 4100

# Phnom Penh's khans (districts) -- the coarser administrative unit.
# Keyed by the substring to look for in the message (Khmer or English) ->
# the canonical name used for display and geocoding. Matched case-
# insensitively (case doesn't apply to Khmer script, so lowering is a
# harmless no-op there).
KHANS = {
    "chamkarmon": "Chamkarmon", "chamkar mon": "Chamkarmon",
    "daun penh": "Daun Penh",
    "sen sok": "Sen Sok", "sensok": "Sen Sok",
    "chroy changvar": "Chroy Changvar", "chroy changva": "Chroy Changvar",
    "chbar ampov": "Chbar Ampov",
    "mean chey": "Mean Chey", "por sen chey": "Por Sen Chey", "pou senchey": "Por Sen Chey",
    "prek pnov": "Prek Pnov", "dangkao": "Dangkao",
    "toul kork": "Toul Kork", "tuol kork": "Toul Kork",
    "សែនសុខ": "Sen Sok",
    "ព្រែកព្នៅ": "Prek Pnov",
    "ឫស្សីកែវ": "Russey Keo", "ឬស្សីកែវ": "Russey Keo",  # both spellings seen in the wild
    "ដូនពេញ": "Daun Penh",
    "ទួលគោក": "Toul Kork",
    "ចំការមន": "Chamkarmon",
    "មានជ័យ": "Mean Chey",
    "ចោមចៅ": "Por Sen Chey",
    "ដង្កោ": "Dangkao",
    "ជ្រោយចង្វារ": "Chroy Changvar",
    "ច្បារអំពៅ": "Chbar Ampov",
    "៧មករា": "7 Makara",
}

# Sangkats (communes) -- one administrative level more precise than a
# khan. When a post names both its sangkat and its khan (the very common
# "សង្កាត់<sangkat> ខណ្ឌ<khan>" pattern), the sangkat alone is precise
# enough to geocode confidently even with no Google Maps link at all --
# see sync.py's qualifies-for-a-pin gate.
SANGKATS = {
    "bkk1": "BKK1", "bkk2": "BKK2", "bkk3": "BKK3", "boeung keng kang": "BKK1",
    "toul tom poung": "Toul Tom Poung", "tuol tom poung": "Toul Tom Poung",
    "russian market": "Toul Tom Poung",
    "tonle bassac": "Tonle Bassac",
    "គោករកា": "Kouk Roka",
    "ភ្នំពេញថ្មី": "Phnom Penh Thmei",
    "ឃ្មួញ": "Khmuonh",
    "ក្រាំងធ្នង់": "Krang Thnong",
    "ទួលសង្កែ": "Tuol Sangke",
    "ច្រាំងចំរេះ": "Chrang Chamres",
    "បឹងកេងកង": "BKK1",  # Khmer original of the "BKK" abbreviation
}

# Informal/colloquial area names and provincial towns -- not part of the
# khan/sangkat pairing above (either not a strict administrative unit, or
# a whole town/province rather than a Phnom Penh sangkat), but still
# useful for general display/filtering in extract_location().
OTHER_AREAS = {
    "riverside": "Riverside, Phnom Penh",
    "diamond island": "Koh Pich, Phnom Penh", "koh pich": "Koh Pich, Phnom Penh",
    "olympic": "Olympic Stadium, Phnom Penh",
    "siem reap": "Siem Reap", "sihanoukville": "Sihanoukville", "preah sihanouk": "Sihanoukville",
    "battambang": "Battambang", "kampot": "Kampot", "kep": "Kep",
    "kampong cham": "Kampong Cham", "kampong speu": "Kampong Speu", "kandal": "Kandal",
    "phnom penh": "Phnom Penh",
    "តាខ្មៅ": "Ta Khmau, Kandal",
}

# Real Khmer-language posts almost never use the English words below --
# "ដី" (land), "លក់" (sell/for sale), "ជួល" (rent) are what actually shows
# up. Without these, property type and sale/rent came back blank for
# nearly every real (non-English) listing.
PROPERTY_TYPES = {
    "condo": "condo",
    "condominium": "condo",
    "apartment": "apartment",
    "flat": "apartment",
    "villa": "villa",
    "borey": "villa",
    "house": "house",
    "shophouse": "shophouse",
    "shop house": "shophouse",
    "land": "land",
    "office": "office",
    "room": "room",
    "studio": "apartment",
    "ដី": "land",
    "វីឡា": "villa",
    "វិឡា": "villa",
    "ផ្ទះ": "house",
    "អាផាតមិន": "apartment",
    "កុងដូ": "condo",
    "ការិយាល័យ": "office",
    # Commercial structures. These used to fall through to "land" (the
    # "ដី" in "ទំហំដី: land size" came first), so a warehouse or building
    # for sale/rent was counted as land. (extract_property_type breaks
    # position ties toward the longer keyword, so "ផ្ទះសំណាក់" beats "ផ្ទះ".)
    "warehouse": "warehouse",
    "ឃ្លាំង": "warehouse",
    "factory": "factory",
    "រោងចក្រ": "factory",
    "hotel": "hotel",
    "សណ្ឋាគារ": "hotel",
    "guesthouse": "hotel",
    "guest house": "hotel",
    "ផ្ទះសំណាក់": "hotel",
    "building": "building",
    "អគារ": "building",
}

RENT_WORDS = ["for rent", "to rent", "rent", "lease", "monthly", "ជួល"]
SALE_WORDS = ["for sale", "sale", "urgent sale", "selling", "លក់"]

# $1,200 | $1200 | USD 1200 | 1200 usd | 1.2k usd
# The `\b` before the second alternative's digit group matters: without
# it, a trailing digit in an unrelated token (e.g. the "1" in "BKK1")
# could kick off a match that then finds a "$" or "usd" later in the
# string, well before the regex ever reaches the real price.
PRICE_USD_RE = re.compile(
    r"(?:usd\s*\$?|\$)\s*([\d,]+(?:\.\d+)?)\s*k?\b|\b(\d[\d,]*(?:\.\d+)?)\s*k?\s*(?:usd|\$)",
    re.IGNORECASE,
)
# 5,000,000 riel | 5 million riel | ៛5,000,000
PRICE_KHR_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(?:million\s*)?(?:riel|khr|៛)",
    re.IGNORECASE,
)

BEDROOM_RE = re.compile(r"(\d+)\s*(?:bed(?:room)?s?|br\b)", re.IGNORECASE)

# 120 sqm | 120 sq m | 120 sq.m | 120m2 | 120 m² | 6,218 m² | 120 square meters
SIZE_SQM_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(?:sq\s*\.?\s*m(?:eters?)?\b|sqm\b|m2\b|m²)",
    re.IGNORECASE,
)
# 5x20m | 5 x 20 m | 5m x 20m  (common way land plots are described)
SIZE_DIMENSIONS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*m?\s*[x×]\s*(\d+(?:\.\d+)?)\s*m\b",
    re.IGNORECASE,
)

PHONE_RE = re.compile(r"(?:\+?855|0)[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{3,4}")
TELEGRAM_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{4,}")

# A real Google Maps link the poster themselves dropped a pin at and
# included -- e.g. "Map: https://maps.app.goo.gl/xyz". This is much more
# precise than extract_location()'s neighborhood-level guess since it's
# exactly where the poster placed it, not a district centroid. Some
# Telegram channels include this on nearly every post; others (almost)
# never do -- see find_similar_channels.py for finding more of the former.
MAP_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:maps\.app\.goo\.gl/\S+|goo\.gl/maps/\S+|(?:maps\.)?google\.[a-z.]+/maps\S*)",
    re.IGNORECASE,
)


def extract_price(text):
    """Returns (value_in_usd_or_None, currency_or_None, raw_matched_substring)."""
    m = PRICE_USD_RE.search(text)
    if m:
        raw = m.group(0)
        num = m.group(1) or m.group(2)
        value = float(num.replace(",", ""))
        if "k" in raw.lower():
            value *= 1000
        return value, "USD", raw

    m = PRICE_KHR_RE.search(text)
    if m:
        raw = m.group(0)
        num = float(m.group(1).replace(",", ""))
        if "million" in raw.lower():
            num *= 1_000_000
        return round(num / KHR_PER_USD, 2), "KHR", raw

    return None, None, None


def extract_khan(text):
    lower = text.lower()
    for needle, canonical in KHANS.items():
        if needle in lower:
            return canonical
    return None


def extract_sangkat(text):
    lower = text.lower()
    for needle, canonical in SANGKATS.items():
        if needle in lower:
            return canonical
    return None


def extract_location(text):
    """General-purpose display location for the dashboard/filters: the
    sangkat if we found one (more precise), else the khan, else an
    informal area/provincial name. See extract_khan/extract_sangkat for
    the two separately-tracked fields the map-pin gate actually cares
    about (see sync.py)."""
    sangkat = extract_sangkat(text)
    if sangkat:
        return sangkat
    khan = extract_khan(text)
    if khan:
        return khan
    lower = text.lower()
    for needle, canonical in OTHER_AREAS.items():
        if needle in lower:
            return canonical
    return None


# Structure keywords describe what a post is selling only when they stand
# on their own ("ឃ្លាំងសម្រាប់ជួល" = warehouse for rent). Land posts often
# describe neighbors -- "មុខបុរី ... និង រោងចក្រ នានា" (facing a borey and
# various factories) -- so an occurrence right after one of these words is
# ignored.
_STRUCTURE_TYPES = {"warehouse", "factory", "hotel", "building"}
_NEIGHBOR_WORDS = ("ជិត", "មុខ", "ជាប់", "ក្បែរ", "និង", "ម្តុំ", "តំបន់", "near", "next to",
                   "beside", "facing", "opposite", "around", "and ")


def _find_keyword(lower, kw, normalized):
    """Position of the first usable occurrence of kw, or -1."""
    start = 0
    while True:
        pos = lower.find(kw, start)
        if pos == -1:
            return -1
        if normalized not in _STRUCTURE_TYPES:
            return pos
        before = lower[max(0, pos - 14):pos].rstrip(" \u200b")
        if not any(before.endswith(w.rstrip()) for w in _NEIGHBOR_WORDS):
            return pos
        start = pos + len(kw)


def extract_property_type(text):
    """Picks whichever keyword occurs EARLIEST in the text, not whichever
    is checked first in PROPERTY_TYPES -- these posts almost always name
    what's actually being sold up front, then mention other structures
    in passing (most commonly: a house listing stating its land size,
    e.g. "ទំហំដី: 15m x 50m", would otherwise always get overridden to
    "land" just because "ដី" happens to come first in the dict, even
    though "ផ្ទះ" (house) appeared earlier in the actual post). Ties on
    position go to the longer keyword ("ផ្ទះសំណាក់" beats "ផ្ទះ")."""
    lower = text.lower()
    best = None
    for kw, normalized in PROPERTY_TYPES.items():
        pos = _find_keyword(lower, kw, normalized)
        if pos != -1 and (best is None or (pos, -len(kw)) < best[0]):
            best = ((pos, -len(kw)), normalized)
    return best[1] if best else None


def extract_bedrooms(text):
    m = BEDROOM_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def extract_size(text):
    """Returns a normalized size string like '120 sqm' or '5x20m', or
    None. Covers the two common phrasings: a floor-area figure (condos,
    apartments, houses) and plot dimensions (land)."""
    m = SIZE_SQM_RE.search(text)
    if m:
        return f"{m.group(1)} sqm"
    m = SIZE_DIMENSIONS_RE.search(text)
    if m:
        return f"{m.group(1)}x{m.group(2)}m"
    return None


def extract_listing_kind(text):
    """Real posts almost always state sale-vs-rent in the opening phrase
    ("លក់បន្ទាន់" / "For rent: ..."), so check there first, and prefer a
    sale match over a rent match. Otherwise a for-sale listing that
    mentions its existing rental income later on as a selling point
    (a common pattern -- "already earning $430/month in rent") gets
    misread as a rental itself, since "rent" only needs to appear
    somewhere in the whole message."""
    headline = text[:80].lower()
    for source in (headline, text.lower()):
        if any(w in source for w in SALE_WORDS):
            return "sale"
        if any(w in source for w in RENT_WORDS):
            return "rent"
    return None


def extract_contact(text):
    phones = PHONE_RE.findall(text)
    handles = TELEGRAM_HANDLE_RE.findall(text)
    parts = []
    if phones:
        parts.append(phones[0])
    if handles:
        parts.append(handles[0])
    return " / ".join(parts) if parts else None


def extract_map_link(text):
    m = MAP_LINK_RE.search(text)
    return m.group(0).rstrip(").,;!。") if m else None


def make_dedup_hash(price_value, location, property_type, text):
    """Rough cross-post detector: same rounded price + location + type +
    a chunk of normalized text. Not exact -- two different units that
    happen to share all of these will collide, and reworded reposts of
    the same unit will NOT collide. Good enough as a first filter; treat
    the dashboard's dedup as "probably the same," not certain."""
    normalized_text = re.sub(r"\s+", " ", text.strip().lower())[:120]
    price_bucket = round(price_value / 10) * 10 if price_value else "na"
    key = f"{price_bucket}|{location}|{property_type}|{normalized_text}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def parse_message(text):
    """Runs all extractors and returns a dict of fields ready to merge
    into a listing row. Always returns something, even for messages that
    aren't really listings -- the caller decides whether to keep it
    (see listener.py's should_keep filter)."""
    price_value, price_currency, price_raw = extract_price(text)
    khan = extract_khan(text)
    sangkat = extract_sangkat(text)
    location = extract_location(text)
    property_type = extract_property_type(text)
    bedrooms = extract_bedrooms(text)
    size_text = extract_size(text)
    listing_kind = extract_listing_kind(text)
    contact = extract_contact(text)
    source_maps_link = extract_map_link(text)
    dedup_hash = make_dedup_hash(price_value, location, property_type, text)

    return {
        "price_value": price_value,
        "price_currency": price_currency,
        "price_raw": price_raw,
        "location": location,
        "khan": khan,
        "sangkat": sangkat,
        "property_type": property_type,
        "bedrooms": bedrooms,
        "size_text": size_text,
        "listing_kind": listing_kind,
        "contact": contact,
        "source_maps_link": source_maps_link,
        "dedup_hash": dedup_hash,
    }


def looks_like_listing(text, parsed):
    """Simple gate so the DB doesn't fill up with 'thanks!' and group
    chatter. A message counts as a listing candidate if it has a price
    OR (a property type AND a location) -- tune this if your groups
    have a different style (e.g. always include a keyword like
    'available now')."""
    if not text or len(text.strip()) < 10:
        return False
    if parsed["price_value"] is not None:
        return True
    if parsed["property_type"] and parsed["location"]:
        return True
    return False
