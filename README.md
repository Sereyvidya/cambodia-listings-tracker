# Cambodia Listings Tracker

Pulls real estate listings out of Telegram groups/channels and shows them
in one filterable local dashboard. Starting point for the wider "compile
all listings in one place" idea — this version only covers Telegram
(the easiest and lowest-risk source to automate); real estate websites and
Facebook groups are natural next additions, but each needs its own
separate piece of work (see "Where this could go next" below).

> **Which sources we collect, what we exclude from each, and why:** see [SOURCES.md](SOURCES.md).

## What you get

- `listener.py` — logs into Telegram and pulls listing-like messages
  from the groups you configure, storing them in a local SQLite database.
- `dashboard.py` — a small local website (Flask) to browse, filter, and
  search everything that's been collected, with photos.
- `extract.py` — the (regex-based) logic that guesses price, size,
  location, property type, bedrooms, sale-vs-rent, and contact info out
  of each message's raw text.
- `sync.py` — publishes what's been collected: geocodes each listing's
  location into a Google Maps pin, pushes qualifying listings to a
  Google Sheet, posts them into a Telegram group, and refreshes a Google
  Earth file. See "Publishing what's collected" below.

Nothing here is hosted anywhere — it all runs on whatever computer you
start it on. That's fine to start with; see the hosting note at the
bottom for when you outgrow that.

## One-time setup

1. **Install Python dependencies** (Python 3.9+):
   ```
   pip install -r requirements.txt
   ```

2. **Get Telegram API credentials.** Go to https://my.telegram.org, log
   in with the Telegram account you want the listener to use (your dad's
   own account is simplest, since he's already a member of the relevant
   groups), click "API development tools", and create an app. You'll get
   an `api_id` (a number) and `api_hash` (a string) — you only do this once.

3. **Configure.** Copy `config.example.yaml` to `config.yaml` and fill in:
   - `telegram.api_id`, `telegram.api_hash`, `telegram.phone` from step 2
   - `groups`: the list of Telegram group/channel usernames to monitor
     (the account must already be a member of each one — join them
     normally in the Telegram app first)

   `config.yaml` is not meant to be shared or committed anywhere — it
   holds real login credentials.

4. **First login.** Run:
   ```
   python listener.py backfill
   ```
   The first time, Telethon will ask for the login code Telegram just
   texted/sent to that account, and possibly a 2FA password if one is
   set. After that it saves a local session file
   (`<session_name>.session`) so you won't be asked again on future runs.
   This also does the first pull of history for each group -- a longer
   window the very first time a given channel is backfilled
   (`settings.first_time_backfill_days`), then a shorter one on every
   run after that (`settings.backfill_days`), tracked automatically per
   channel so adding a new group later doesn't affect the others.

## Running it day to day

Two separate things you can run independently:

- **Keep collecting new listings:**
  ```
  python listener.py listen
  ```
  This stays running and stores new messages as they arrive. Leave it
  running in a terminal tab, or under `tmux`/`screen`/`nohup` if you want
  it to survive closing your terminal. It does nothing while it isn't
  running — there's no background scheduling here yet.

- **Browse what's been collected:**
  ```
  python dashboard.py
  ```
  Then open http://127.0.0.1:5000. Filter by price, location, property
  type, sale/rent, source group, whether a listing has a map pin, or
  free-text search. "Hide" removes a listing from view (e.g. junk that
  got misclassified as a listing) without deleting it from the database.
  Click **view as map** for a live clustered map of every geocoded
  listing (free OpenStreetMap tiles, no API key) -- this doesn't need
  `sync.py`'s Sheets/Telegram/Earth steps, just geocoding to have run.

You can run both at once in two terminal tabs.

## Publishing what's collected (Sheets, Maps/Earth, Telegram)

`sync.py` is a separate, optional step that takes what `listener.py` has
already stored locally and publishes it out to the places your dad
actually wants to look:

```
python sync.py
```

Run it by hand whenever you want to push out what's new (or put it on a
cron/launchd schedule yourself — there's no built-in scheduler here, same
as `listener.py`). It's safe to re-run any time; each destination only
acts on rows it hasn't already handled.

What it does, in order:

1. **Geocode.** Turns each listing's parsed `location` (e.g. "BKK1")
   into map coordinates, using OpenStreetMap's free Nominatim service —
   no API key or billing needed. Results are cached (`geocode_cache`
   table), so this only ever looks up each distinct location string
   once, not once per listing.
2. **Google Sheets.** Pushes listings that have a Google Maps pin (see
   below — untagged locations are skipped, per "only wants properties
   with a map link") into a Sheet, one row per listing: description,
   price, size, location, type, sale/rent, bedrooms, contact, who
   posted, source, date, and the Maps link. Cross-posted duplicates
   (same `extract.make_dedup_hash()`) are only added once.
3. **Telegram notify.** Posts the same qualifying, not-yet-notified
   listings into a Telegram group of your choosing, so your dad can
   check new listings right from Telegram.
4. **Google Earth export.** Regenerates `cambodia_listings.kmz` — every
   qualifying listing as a pin, with its first photo and full details in
   the popup.

Each of these is independently on/off in `config.yaml` — turn on the
ones you want as you set them up.

### Google Sheets setup

Sheets access uses a Google **service account** (a robot login, not your
personal Google account), since this runs unattended:

1. In [Google Cloud Console](https://console.cloud.google.com), create a
   project (or reuse one), then enable the **Google Sheets API**.
2. Under "IAM & Admin" → "Service Accounts", create a service account.
   Create a JSON key for it and download it — save it into this folder
   as `service_account.json` (matches `google_sheets.service_account_file`
   in `config.yaml`; this file holds credentials, never commit it —
   `.gitignore` already excludes it).
3. Create a Google Sheet (or reuse one) for your dad to view. Share it
   with the service account's email address (looks like
   `something@your-project.iam.gserviceaccount.com`, found in the JSON
   key file or the Cloud Console) as an **Editor**.
4. Copy the sheet's ID out of its URL —
   `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit` — into
   `google_sheets.spreadsheet_id` in `config.yaml`, and set
   `google_sheets.enabled: true`.

### Telegram notify setup

No new credentials needed — it reuses the same Telegram account as
`listener.py`, just a second login session so the two don't collide
(you'll get one extra login-code prompt the first time). In
`config.yaml`, set `notify.enabled: true` and `notify.target_chat` to
the group/channel username (without `@`) you want new listings posted
into — the account must already be a member.

### Google Maps / Google Earth — what's automatic vs. manual

- **Per-listing Google Maps link**: fully automatic — every listing that
  geocodes successfully gets a clickable Maps link (shown in the
  dashboard, the Sheet, the Telegram messages, and the Earth export).
- **Google Earth with photos**: fully automatic — open
  `cambodia_listings.kmz` in Google Earth Pro (double-click it), or at
  [earth.google.com](https://earth.google.com) via Projects → Import
  KML file. Each pin's popup shows the listing's photo and details.
- **A custom "My Maps"**: Google retired the public API for My Maps
  years ago, so there's no way to keep one updated automatically. The
  practical equivalent: open [mymaps.google.com](https://mymaps.google.com),
  create a map, and use Import → pick `cambodia_listings.kmz` (the same
  file Earth uses) — a few clicks, re-import whenever you want it
  refreshed with new listings.

## Known limitations (by design, for a first version)

- **Extraction is heuristic**, not perfect. It's tuned for common
  Cambodian real estate phrasing (USD prices, common Phnom Penh
  neighborhood names, "bed"/"BR" for bedrooms, etc.) — see the constants
  at the top of `extract.py` to extend it as you see real messages come
  through. When it can't confidently parse a field, it just leaves it
  blank rather than guessing wildly; the raw message text is always kept
  so nothing is lost.
- **Dedup is approximate.** The same unit often gets posted to multiple
  groups with slightly different wording. `extract.make_dedup_hash()`
  buckets by rounded price + location + property type + a text snippet,
  which catches obvious cross-posts but isn't exact in either direction.
- **Riel prices are converted at a fixed rate** (`KHR_PER_USD` in
  `extract.py`) since Cambodia is heavily USD-dollarized and most real
  listings are already in USD — update that constant occasionally.
- **No scheduling / not always-on.** `listener.py listen` only collects
  while it's actively running on a machine that's on. Same for
  `sync.py` — it publishes whatever's new at the moment you run it.
- **A listing gets a pin one of two ways** (see `db._PUBLISH_GATE_SQL`
  and `db.pin_info`), and the map/Sheet/Telegram distinguish which:
  - **A real link the poster included** (`extract_map_link` finds an
    actual `maps.app.goo.gl`/`google.com/maps` URL in the post) —
    precise, wherever they dropped the pin. Shown in green.
  - **Both a sangkat (commune) and khan (district) named in the post**
    (e.g. "សង្កាត់គោករកា ខណ្ឌព្រែកព្នៅ") — per your dad, a sangkat is
    precise enough to trust even with no map link, but a khan alone
    isn't. Geocoded to that sangkat's centroid, shown in blue, and
    every output notes it's approximate. A khan mentioned without a
    sangkat, or a sangkat without a khan, doesn't qualify.
  - Anything else gets no pin and is left out of the Sheet/Telegram/
    Earth/published-site publishing (the local dashboard still shows
    it, flagged "no map pin", filterable via "Publishable pin").
  - Either way, geocoding is at most sangkat-level, not the actual
    building — good enough to orient someone, not turn-by-turn precise.

## Sharing a live link

The dashboard itself (`dashboard.py`) only runs on your machine, but you
can publish a shareable, permanently-hosted snapshot for free via GitHub
Pages -- no server to keep running, no cost:

```
python3 publish_static.py
```

This exports every map-qualified listing (same set as Sheets/Telegram/
Earth) plus their photos into `docs/`, as a self-contained static site
(no backend -- filtering happens in the browser via JavaScript over
`docs/listings.json`). Commit and push `docs/` to publish an update:

```
git add docs/listings.json docs/photos
git commit -m "Update published listings"
git push
```

This repo is public on GitHub Pages at
**https://sereyvidya.github.io/cambodia-listings-tracker/** (list view)
and `/map.html` (clustered map view). Being public means the exact link
isn't password-protected -- fine here since all the underlying data
already comes from public Telegram channels -- but it also isn't
indexed or listed anywhere. Nothing in `docs/` ever includes
`config.yaml`, session files, or the Google service account key; those
stay local (see `.gitignore`).

This snapshot doesn't auto-update -- it reflects whatever `listings.db`
looked like the last time you ran `publish_static.py` and pushed. A
natural next step if that becomes annoying: move the database itself to
a hosted backend (e.g. Supabase's free tier) so the site reads live
data directly instead of a periodically-republished snapshot -- that's
a real rewrite of `db.py` though, not a small change, and still
wouldn't run `listener.py` for you (that's still a persistent process
that needs a machine to run on).

## Where this could go next

- **More real estate websites**: each needs its own scraper since every
  site has different HTML. `scrape_aps.py` (aps.com.kh land listings) is
  the first: run it with no flags for a dry run, `--commit` to insert,
  then `sync.py` / `publish_static.py` as usual. APS has no real map
  pins, but each page carries a per-listing coordinate rounded to ~1 km
  (longitude first, so it's un-swapped and checked against the
  listing's province); those become blue "approximate" pins, with a
  commune + district title as the fallback.
  `scrape_pointer.py` (pointerasia.com, which feeds khpropertyhub.com's
  listings) and `scrape_ips.py` (ips-cambodia.com) work the same way: both
  sites publish a deliberately blurred per-listing coordinate (Pointer
  ~150-500 m), used as a blue pin once it passes the province check. IPS
  stamped most of its listings with one bulk migration date, so by default
  only listings with a genuine date are imported (`--include-undated` to
  override). All three take `--commit`, `--days` (default 183) and read
  each site's own sitemap rather than crawling its search pages.
  Sites behind bot protection (e.g. khpropertyhub.com's Cloudflare
  challenge) are skipped rather than worked around.
- **Facebook groups**: technically the hardest and riskiest (Facebook's
  terms prohibit automated scraping and can ban the automating account).
  Worth revisiting once you know whether Telegram + websites alone cover
  most of what your dad needs.
- **Always-on hosting**: once this is useful enough to want running
  24/7 without a laptop being on, it can move to a small always-on
  server (a cheap VPS, ~$5-6/month) — same code, just deployed instead
  of run locally.
- **Better dedup / alerts**: e.g. filtering the Telegram notify step to
  only ping for listings matching your dad's saved criteria, instead of
  everything that qualifies.
- **Scheduling `sync.py`**: right now it's a manual/cron-it-yourself
  step, same as `listener.py listen`.
