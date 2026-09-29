"""
Turns a rough location string (e.g. "BKK1") into map coordinates, using
OpenStreetMap's free Nominatim geocoder -- no API key or billing needed.

Results are cached in listings.db (see db.get_cached_geocode /
set_cached_geocode) so any given location string is only ever looked up
once: there are only a few dozen distinct entries in extract.LOCATIONS,
so the whole backlog costs at most a few dozen requests, one time.

Nominatim's usage policy caps free requests at ~1/second and asks for an
identifying User-Agent -- both are handled here.
"""

import re
import time

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "cambodia-listings-tracker/1.0 (personal/family project)"
MIN_INTERVAL_SECONDS = 1.1

# Google Maps share links (often maps.app.goo.gl short links) redirect to
# a final URL that encodes the exact coordinates the poster dropped a pin
# at, in one of a few formats depending on the link type. Try the most
# specific/reliable first.
_COORD_PATTERNS = [
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),  # place-detail URLs
    re.compile(r"/place/(-?\d+\.\d+),(-?\d+\.\d+)"),  # bare-coordinate place URLs
    re.compile(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)"),  # "dropped pin" share URLs (?q=lat,lon)
    re.compile(r"[@,](-?\d+\.\d+),(-?\d+\.\d+)"),  # @lat,lon,zoom map-view URLs
]

_last_request_time = 0.0


def _rate_limit():
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_request_time = time.monotonic()


def geocode(location_text, region_hint="Cambodia"):
    """Returns (lat, lon) or None if it couldn't be resolved."""
    query = f"{location_text}, {region_hint}" if region_hint else location_text
    _rate_limit()
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as e:
        print(f"  geocoding failed for {location_text!r}: {e}")
        return None
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def maps_link(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def resolve_source_map_link(url, timeout=10):
    """Follows a Google Maps link the poster themselves included to its
    final destination and pulls out the precise lat/lon it encodes.
    Returns (lat, lon), or None if it couldn't be resolved -- e.g. a
    network hiccup, or Google changes their URL format. This is a plain
    HTTP redirect fetch, no API key needed (same as opening the link in a
    browser); unlike geocode(), there's no meaningful rate limit to
    respect since each link is only ever resolved once (see
    db.get_cached_geocode)."""
    try:
        resp = requests.get(
            url,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        final_url = resp.url
    except requests.RequestException as e:
        print(f"  couldn't resolve map link {url!r}: {e}")
        return None
    for pattern in _COORD_PATTERNS:
        m = pattern.search(final_url)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None
