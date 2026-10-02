# Sources and exclusion log

One place that records, for every website / Telegram channel on your dad's
list, **what we collect, what we leave out, and why**. Update the relevant
section whenever a source is processed or a rule changes -- the point is
that nobody has to re-derive "why isn't X in the map?" later.

Counts are from the last run (2026-10-02) and will drift as listings age. Totals then: 1,160 published pins (360 green real-link, 800 blue approximate).

## Rules that apply to everything

| Rule | Where it's enforced | Why |
|---|---|---|
| Only listings updated in the **last ~6 months** (`--days 183` for websites; `first_time_backfill_days: 180` once per new Telegram channel, then `backfill_days: 10`) | scrapers / `listener.py` | Dad: use the latest posts; started at 1 year, then "just the latest 6 months for now" |
| **Land only**, in Phnom Penh, Kandal, Takeo, Kampong Speu, Kampot, Kep, Sihanoukville, Kampong Chhnang, Siem Reap, Mondulkiri | website scrapers (`webscrape.PROVINCES`) | Dad's criteria for the new list. Earlier Telegram channels were collected before this rule existed and aren't limited to these provinces; most are overwhelmingly land, but e.g. Ramborealestate is mostly houses (filter by type in the dashboard) |
| **Not "land with a building"** ("Land and Building", "Land with House and Warehouse", "Land and Warehouse") | `webscrape.has_structures()` | Dad asked for plain land. "Land with Title" and similar are kept |
| **A listing is only published if it has a pin**: real Google Maps link from the post (green), or both sangkat AND khan named (blue), or a coordinate supplied by the listing site (blue) | `db._PUBLISH_GATE_SQL` | Dad: only properties with a map link; relaxed to sangkat + khan, and later to site-supplied coordinates. A khan alone or a sangkat alone doesn't qualify |
| Site coordinates must fall **inside the listing's own province** (distance from the province centre) | `webscrape.coordinate_plausible()` | Catches placeholder / wrong coordinates (see APS Kep, IPS below); the listing falls back to commune + district geocoding or gets no pin |
| **Duplicates**: the same property reposted collapses to its newest copy; a map link reused across several different prices excludes the whole group | `dedupe_keep_newest()` (websites), `db.compute_duplicate_link_exclusions` (Telegram) | Dad: avoid duplicates, avoid reused reference links |
| Scraping is **polite**: robots.txt respected, honest User-Agent, 1 request/second, no bot-protection workarounds | `webscrape.fetch()` | Family project; we don't circumvent Cloudflare etc. |

"Excluded" below means *not stored or hidden*. A listing can also be stored
but unpublished (no pin) -- it stays in the local dashboard under "no map pin".

## Websites

### realestate.com.kh -- skipped
Dad's list included it, but it has its own map, so there's nothing for us to
add. Revisit only if he wants it merged into this view.

### khpropertyhub.com -- skipped (blocked)
Cloudflare bot challenge on every page. We don't work around that. Its
listings use the same property IDs as Pointer (pointerasia.com), so they're
covered indirectly through the Pointer scraper.

### aps.com.kh -- done (`scrape_aps.py`) -- 33 kept, 30 published
- **Pin source:** each page's "Open in google map" link is `?q=<lon>,<lat>`
  (longitude first, rounded to ~1 km) -- not a real pin, but a usable
  approximate coordinate after un-swapping. Blue.
- **Excluded:**
  - 41 pages older than 6 months.
  - 6 pages outside the target provinces.
  - 4 pages that don't parse as a listing (old "-2" duplicate URLs for
    hectare-sized plots near the Vietnamese border, returning an empty page).
  - 3 "Land and Building" / "Land and Warehouse" listings (hidden after the
    structures rule was added -- they'd slipped through the first run).
  - 1 site coordinate rejected: a Kep listing whose coordinate
    (11.47, 105.13) is ~130 km away in Kandal; it's stored without a
    site-coordinate pin.
- **Kept but unpinned:** the few listings with neither a plausible
  coordinate nor both commune + district (e.g. "Chrouy Changva" on its own).

### pointerasia.com -- done (`scrape_pointer.py`) -- 111 kept, 103 published
- **URLs:** Pointer's own sitemap, only `/property/land-...` slugs naming a
  target province (388 pages). robots.txt disallows `/api/` and `/_next/`;
  those are never touched.
- **Pin source:** the page's "preview location" -- a deliberately blurred
  point with a 70-500 m boundary circle (500 m typical). Blue / approximate.
- **Excluded:**
  - 270 pages updated more than 6 months ago.
  - 5 "land with house/warehouse/factory/villa" titles.
  - 1 archived/inactive listing.
  - 1 repost (same price, size, commune, province) -- newest kept.
  - Other provinces (e.g. Battambang) are never fetched: the slug names a
    province we don't collect.
- **Kept but unpinned (8):** no site coordinate and the commune couldn't be
  found by the geocoder.
- Both sale and rent are kept (39 rent, 72 sale); dad's rule says "land",
  not "sale only".

### ips-cambodia.com -- done (`scrape_ips.py`) -- 143 kept, 142 published
- **URLs:** `sitemap_index.xml` -> the area sitemaps for target provinces
  (62 of them; 1,954 land URLs). `?page=` / `?listing_page=` pagination is
  disallowed in robots.txt and never used.
- **Pin source:** `data-lat` / `data-lng` behind the page's "radius map" --
  blurred by the site, so blue / approximate.
- **Excluded:**
  - **~1,790 pages carrying a bulk migration date** (sitemap lastmod
    2026-05-19 ... 2026-06-23). The site stamped most listings with these
    dates, so their real age is unknowable and the "latest posts" rule can't
    be checked. Not fetched by default; `--include-undated` fetches them
    (~30 min). **Open question for dad:** are these worth importing anyway?
  - 20 URLs whose slug says building / warehouse / factory / "-with-"
    (+1 "Commercial Land and House" found on the page and hidden).
  - Page "Property Type" must contain "land" (e.g. "Commercial Land" is OK).
  - 6 site coordinates rejected as implausible -- most were a shared
    placeholder point (12.5657, 104.9910) that appears on listings in both
    Phnom Penh and Siem Reap. Those listings were pinned from commune +
    district instead (5), or left unpinned (1, "Srah Chork").
- Listing dates use the page's `dateModified` (falling back to
  `datePublished`); the sitemap's `lastmod` matches it.
- Re-runs fetch only pages that are new or whose sitemap `lastmod` is newer
  than when they were stored.

## Telegram channels

All collected with `listener.py` (Telethon user session, per-channel
180-day first backfill, then 10 days). Every channel here is Khmer-language
(nearly all posts are Khmer text), posted by the channel itself, with
photos on most posts. A Telegram listing is published only if it has a
real map link or both sangkat + khan, and isn't a reused/duplicate link.

| Channel | Collected | Published | What it posts / quirks |
|---|---|---|---|
| poekhachrealestate | 884 | 197 | On dad's list. ~94% land, all for sale. 45% of posts have a real map link, almost none name sangkat + khan. 220 rows flagged as duplicate/reused map links; 28 more links are place-name searches (see below) |
| Ramborealestate | 382 | 48 | **Mostly houses** (252 house / 101 land / 26 villa), so few qualify as land. 9% real links, 9% sangkat + khan |
| diamond_property | 348 | 308 | First channel we tried. ~99% land. Rarely includes a map link (4%) but 87% of posts name sangkat + khan, so it publishes as blue pins |
| hotsales061702070 | 274 | 188 | ~95% land; 23% real links, 54% sangkat + khan; a handful of rentals |
| sokthon2024 | 150 | 108 | Added at your request. ~97% land; 89% have a real map link (the best green-pin source) |
| kimhong_kps_realestate | 37 | 30 | Land only, 92% real map links, never names sangkat + khan. Looked weak at first (5 published) only because its link lookups had failed -- see "Map-link resolution" below |
| landforsell789 | 28 | 5 | On dad's list. 21% real links, no sangkat + khan, 7 posts with no clear sale/rent word |
| Land_investment_Good | 5 | 1 | Almost nothing posted |
| century21diamond | 104 | 0 | **Dropped** on your request ("stop considering"); rows hidden |
| AsiaRealEstateCambodia | 387 | 0 | **Dropped** on your request; rows hidden |

Why Telegram posts aren't published: no real Google Maps link and not both
sangkat + khan named; or the link is a reused one (same link on several
posts with different prices); or the link only names a place (below). Posts
with neither stay in the local dashboard ("no map pin") and are never
published.

### Map-link resolution (a bug we found and fixed)
Real map links (`maps.app.goo.gl/...`) are followed to get coordinates.
Until 2026-10-02 `sync.py` marked a link "resolved" even when the lookup
failed, so transient network failures permanently dropped listings from the
map -- about 215 listings with real links had no pin (kimhong_kps_realestate
had 29 of 34 affected). Failed lookups are now stored as `failed:<time>` and
retried every 3 days (`db.RESOLVE_RETRY_DAYS`). Re-running resolved 273 of
305 queued links.

The ~32 that still fail (poekhachrealestate 28, sokthon2024 3,
Ramborealestate 1) are **place-name search links** (`google.com/maps?q=<Khmer
place name>`): they carry text, not coordinates, so there's no pin to read.
They stay unpublished. **Open question for dad:** should those be geocoded
from the place text and shown as blue (approximate) pins?

### Still to do from dad's list (not yet collected)
Channels: @basacrealtycoltd, @Sakhom, @infophnompenhland, សេវាអចលនទ្រព្យ,
@chailinsearrealty, @KoytryPropNexKH, ដីលក់ល្អៗ តម្លៃពិសេសៗ, @sokhunKAT,
@SengHeng_Property, @dealcorecambodia, @Sensoklandpricecenter,
@onelandrealestate168, @somtola007, @leng_enghuo99, @Land_South_City,
@DreamPropertySolution, @kanalmao168, @SevenDaysRealEstate, @pointerproperty
(likely overlaps the Pointer website -- expect duplicates), @propnexcambodiarealestate,
@Land_outskirts, @sokthon2023, @Properties_Mall, @Percentage_Realty.
Groups: S.V Gold Realty Co.Ltd, @Land_Home168, Camlink Properties,
🌹សមាគមន៏ទីផ្សារដីធ្លី 25 ខេត្ត.ក្រុង🌹.

(@poekhachrealestate and @landforsell789 from the same list are already
collected, above. For each new channel, record its language, mix of land vs.
houses, how often it posts a real map link / sangkat + khan, and anything odd.)

Record each one above -- with what was excluded and why -- as it's processed.
