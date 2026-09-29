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

import time

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "cambodia-listings-tracker/1.0 (personal/family project)"
MIN_INTERVAL_SECONDS = 1.1

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
