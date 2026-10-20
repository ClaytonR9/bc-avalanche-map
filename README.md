# BC Mapping - Colorado Avalanche Danger Map

Overlays CAIC danger ratings on USGS topo/imagery with hillshade and 3D terrain.
Click a zone for per-elevation ratings, avalanche problems, and an aspect/elevation rose.

## Deploy (hosted, free)
This repo includes a Render Blueprint (`render.yaml`) for a one-click-ish free deploy:
1. Push this repo to your own GitHub account (already done if you're reading this there).
2. Go to https://render.com, sign up/sign in (free), click **New +** -> **Blueprint**, and
   connect this GitHub repo. Render reads `render.yaml` automatically.
3. During setup, Render will prompt you for the optional secret env vars (satellite + NOAA
   token, same ones as `secrets.env` below) - leave any of them blank to skip that feature;
   everything else works without them.
4. Deploy. You'll get a free `*.onrender.com` URL. Open it on your phone and use your
   browser's "Add to Home Screen" (iOS Safari) or "Install app" (Android Chrome / desktop
   Chrome) to add it like a native app - no app-store account or review needed.

Free-tier notes: the service spins down after inactivity (the first load after a quiet
period takes ~30-60s to wake up), and the filesystem is ephemeral, so the CAIC forecast
archive (`history.db`) resets on redeploys/restarts rather than accumulating forever. If you
want that archive to persist indefinitely, upgrade to a paid Render plan with a persistent
disk (or any host that gives you one) - nothing else about the setup changes.

## Run
    python3 server.py            # live CAIC data (needs internet)
    DEMO=1 python3 server.py     # fake sample zones, for testing the UI
Open http://localhost:8000. No pip installs needed (Python 3.8+).

## Data
- Zones + ratings: https://api.avalanche.org/v2/public/products/map-layer/CAIC
- Problems/bands:  https://api.avalanche.org/v2/public/products?type=forecast&center_id=CAIC
- Each live load saves that day's forecast into history.db (SQLite), so history accumulates
  from the first day you run it. /api/days, /api/forecast?date=YYYY-MM-DD and
  /api/history?zone=ID read it back (no history UI yet).

## Notes / to verify
- Off-season, zones return "no rating" (-1) and no problems.
- The product field names (danger, forecast_avalanche_problems, forecast_zone) and the
  map-layer id == forecast_zone id join were written from docs, not tested against live data.
- Keep it running daily (e.g. cron hitting /api/forecast) to build the archive.
- Third-party display of avalanche info should not alter its meaning; keep the attribution
  and link back to avalanche.state.co.us.

## Per-slope shading (added)
Slopes inside each zone are colored by aspect (8 compass directions) and elevation band
(above / near / below treeline), computed in the browser from free AWS terrain tiles.
- Cells named in an avalanche problem (e.g. "southwest upper") get that band's danger rating.
- Other cells are shown one level lower (minimum Low/green). If a zone lists no problems,
  every slope gets the band rating.
- Only slopes steeper than the cutoff (default 30 deg) are shaded.
- Treeline is fixed at 11,500 ft; near treeline = 11,250-11,750 ft (edit TREELINE_FT / NEAR_FT in static/index.html).
This is a derived visualization, NOT a CAIC product. CAIC does not publish per-slope ratings.

## Satellite imagery (added)
The "Base map" dropdown has two live-updating satellite options, in addition to the static
USGS imagery (which is NAIP aerial photography, refreshed only about once every 3 years per
USGS's own FAQ - fine for a basemap, but it won't show anything recent).

### Daily, low-res (no setup needed)
"Daily satellite" pulls NASA's VIIRS true-color imagery from their free GIBS service
(~250m/pixel, no account, no API key). It shows yesterday's pass (UTC) - new imagery
lands in GIBS a few hours after capture, and the app rolls over to the new date
automatically if you leave the tab open overnight.

### Weekly, hi-res (needs a free account)
"Weekly satellite" pulls real Sentinel-2 imagery (~10m/pixel, ESA/Copernicus) through a
server-side proxy (`/api/sat-tile/{z}/{x}/{y}` in server.py), so it refreshes to whatever
the most recent cloud-permitting pass is - in practice close to Sentinel-2's own ~5 day
revisit, so roughly weekly. This needs a free Copernicus Data Space Ecosystem account:

1. Create a free account at https://dataspace.copernicus.eu (sign up, verify your email).
2. Go to https://shapps.dataspace.copernicus.eu/dashboard/ and sign in with that account.
3. Create OAuth client credentials: in the dashboard, find the OAuth client / API
   credentials section (a self-service "register new OAuth client" option), create one,
   and copy the **Client ID** and **Client Secret** it gives you immediately - the secret
   is normally only shown once.
4. Create a Sentinel Hub configuration: in the same dashboard, open the Configuration
   Utility, create a new configuration based on the Sentinel-2 L2A data source, and copy
   its **Instance ID**. A default configuration should already include a true-color layer
   (often named `TRUE_COLOR` or `TRUE-COLOR-S2L2A` - check the layer name in that
   configuration's layer list).
5. Open `secrets.env` in this folder (plain text, already created with placeholders - NOT
   committed anywhere, NOT something to paste into a chat) and fill in:
       SH_CLIENT_ID=<your client id>
       SH_CLIENT_SECRET=<your client secret>
       SH_INSTANCE_ID=<your instance id>
       SH_LAYER_NAME=<only change if step 4's layer isn't named TRUE_COLOR>
6. Restart the server and pick "Weekly satellite" from the Base map dropdown. The small
   note under the dropdown will say whether it's working or still missing something.

Free accounts get 10,000 "processing units" per month; the server caches each map tile for
the rest of the day it was first requested, so ordinary panning/zooming reuses cached tiles
instead of spending new ones. This is modified Copernicus Sentinel data.

## Snow depth - SNOTEL (added)
The "Snow depth (SNOTEL)" layer (off by default) plots USDA/NRCS SNOTEL stations statewide,
colored by current snow depth using a 7-step gradient (gray = 0", tan/yellow-green for
shallow, blues for moderate, purple for 60"+). Data comes from NRCS's free AWDB REST API
(no key, open CORS) - station list is fetched once, depth readings use a rolling 7-day
window per station and take the latest actual value, since "today" hasn't always posted
yet. Click a station dot for its exact depth, elevation, and a link to its NRCS station page.
Refreshes every 30 minutes while the layer is on.

## Weather stations (added)
The "Weather stations" layer (off by default) shows NWS official airport/ASOS stations
across Colorado as a broadcast-tower icon. We deliberately only show the ~55 ICAO-coded
(K***) stations (e.g. "KAPA / Denver - Centennial Airport") rather than api.weather.gov's
full 2,000+ station list for the state - the rest are almost all crowd-sourced mesonet
sensors with cryptic IDs ("008BH"), generic names ("5719 HWY 9"), and inconsistent
reporting (~40% returned no live data when sampled). The ICAO set gives clean, recognizable
stations that match a real public forecast page.

Click a station for today's forecast (temperature, conditions, wind speed and direction
shown separately, and the detailed forecast text), pulled live from api.weather.gov's
points -> gridpoint forecast endpoints (no key needed, CORS-open, fetched lazily per click -
nothing is pre-fetched for all 55 stations). The panel also links out to that location's
full forecast at forecast.weather.gov. The station list itself is fetched once and cached,
since airports rarely change; only the forecast is fetched fresh per click.

## Historical weather data (added)
Double-click anywhere on the map to bring up a "See historical data for the area" button,
which opens a full-screen chart view for that point with five time-range tabs: Previous 7
Days, This Month, Past 3 Months, Past 6 Months, and Historical Monthly Trends (a 10+ year
average by calendar month, when the nearby station has that much history). Each tab shows
one range at a time (not a crowded all-at-once view), with a card per metric - snow depth,
new snowfall, precipitation, temperature, cloud coverage, wind speed, wind gust, and wind
direction - each with its own icon, headline stat, and chart.

Three free data sources feed this, chosen per metric/range for whichever actually has the
history:
- **Snow depth, new snowfall (derived from day-over-day depth change), precipitation, and
  temperature** come from the nearest USDA/NRCS SNOTEL station (same free AWDB REST API as
  the Snow depth layer above) - most Colorado SNOTEL stations have decades of daily
  history, so these are available on every tab including Historical Monthly Trends.
- **Wind and cloud data for the Previous 7 Days tab** come from the nearest NWS/ICAO
  airport station (same stations as the Weather stations layer above) - free, no key
  needed, but NWS only retains about a week of observation history, which is why this
  source is only used for that one tab.
- **Wind and cloud data for This Month / Past 3 Months / Past 6 Months / Historical Monthly
  Trends** come from NOAA's National Centers for Environmental Information (NCEI) Climate
  Data Online (CDO) API, via a small server-side proxy (`/api/noaa-wx` in server.py) - this
  is the one piece that needs a free account, since the CDO API requires a token and (like
  the Sentinel Hub setup above) the token should never be shipped to the browser:
  1. Get a free token at https://www.ncdc.noaa.gov/cdo-web/token (just an email address,
     arrives instantly, no approval wait).
  2. Open `secrets.env` in this folder and add:
         NOAA_TOKEN=<your token>
  3. Restart the server. If the token is missing, those four cards show "Connect a free
     NOAA climate token (see README) to see this for this time range/monthly trends"
     instead of an error, and every other card (snow depth, new snowfall, precipitation,
     temperature) still works normally.

NOAA's free tier allows 10,000 requests/day and 5/second; the server paces requests to stay
under that and caches each year of a station's data in memory indefinitely (history doesn't
change), so repeated clicks near the same area don't re-fetch from NOAA each time.

**Station selection for wind data**: the server doesn't just use the literal nearest GHCND
station - most nearby stations in Colorado's network are basic COOP stations that only ever
report temperature/precipitation, with wind coming from a much sparser set of airport-type
stations. `find_nearest_ghcnd_station()` searches specifically for a station that reports
wind (datatypes AWND/WSFG/WSF5/WSF2), widening its search radius more aggressively than the
plain-nearest search, since a wind-reporting station can legitimately be much farther away,
especially out in the backcountry.

**Cloud coverage may often say "No data for this period"** even with wind data working fine
- that's expected, not a bug. Cloud-cover reporting (GHCND's ACMH/ACSH datatypes) is
genuinely rare nationwide; most stations, including many airports, don't report it at all
in this dataset. If no nearby station has it, the cloud card will keep saying "No data for
this period" (distinct from "Connect a free NOAA climate token", which only shows when the
token itself is missing/misconfigured).

Verified live (by Clayton, running the real server with his own NOAA token): wind/cloud
cards correctly distinguish "token not configured" vs. "no data at this station" vs. real
data. If wind speed/gust/direction *still* show "No data for this period" everywhere after
this fix, or numbers look clearly wrong (implausible units, etc.), that's worth flagging -
most likely cause would be a field-name or unit mismatch in `_normalize_ghcnd_day()` in
server.py.
