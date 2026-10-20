#!/usr/bin/env python3
"""
BC Mapping - Colorado avalanche danger overlay server.

- Pulls CAIC zone polygons + danger ratings from the public avalanche.org API
- Pulls per-zone forecast details (avalanche problems, elevation-band danger)
- Serves everything as GeoJSON to the map frontend in ./static
- Saves a snapshot of each day's forecast to SQLite so history builds up

Standard library only. Run:   python3 server.py
Demo data (no network):       DEMO=1 python3 server.py
"""
import json, math, os, re, sqlite3, threading, time, urllib.error, urllib.request, urllib.parse
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "history.db")
API = "https://api.avalanche.org/v2/public"
CENTER = "CAIC"
PORT = int(os.environ.get("PORT", "8000"))
DEMO = os.environ.get("DEMO") == "1"
CACHE_SECONDS = 30 * 60

_lock = threading.Lock()
_cache = {"t": 0, "data": None}

# ---------- weekly hi-res satellite (Sentinel-2 via Copernicus Data Space Ecosystem) ----------
# Credentials live in secrets.env (gitignored, never hardcoded, never sent in chat) - see README.
SECRETS_PATH = os.path.join(HERE, "secrets.env")
SH_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SH_WMS_BASE = "https://sh.dataspace.copernicus.eu/ogc/wms"
SAT_TILE_SIZE = 256
_sh_token_cache = {"token": None, "exp": 0}
_sat_tile_cache = {}          # (z, x, y, day) -> (bytes, content_type)
_SAT_TILE_CACHE_MAX = 1500    # rough cap so a long dev session doesn't grow memory unbounded
SAT_TILE_RE = re.compile(r"^/api/sat-tile/(\d+)/(\d+)/(\d+)$")


_SECRET_KEYS = ("SH_CLIENT_ID", "SH_CLIENT_SECRET", "SH_INSTANCE_ID", "SH_LAYER_NAME", "NOAA_TOKEN")


def load_secrets():
    """Tiny KEY=VALUE[.env] parser, stdlib only, for local dev - plus a real-environment-variable
    fallback/override for hosted deploys. Locally you edit secrets.env (gitignored, never
    committed); on a host like Render/Fly/Railway you set these same keys as actual environment
    variables in its dashboard instead - there's no file to write there, and this lets the exact
    same code work in both places. Real env vars win if both are set."""
    out = {}
    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    for k in _SECRET_KEYS:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def get_sh_token():
    """Client-credentials OAuth token for Sentinel Hub / CDSE, cached until ~1 min before expiry."""
    secrets = load_secrets()
    client_id = secrets.get("SH_CLIENT_ID")
    client_secret = secrets.get("SH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "SH_CLIENT_ID / SH_CLIENT_SECRET missing from secrets.env - "
            "sign up at dataspace.copernicus.eu and see the README for setup steps.")
    now = time.time()
    if _sh_token_cache["token"] and now < _sh_token_cache["exp"]:
        return _sh_token_cache["token"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(SH_TOKEN_URL, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Sentinel Hub login failed ({e.code}): check SH_CLIENT_ID/SH_CLIENT_SECRET "
                            f"in secrets.env are current (client secrets can be regenerated/revoked).")
    _sh_token_cache["token"] = tok["access_token"]
    _sh_token_cache["exp"] = now + tok.get("expires_in", 600) - 60
    return _sh_token_cache["token"]


def tile_bbox_3857(z, x, y):
    """Standard slippy-map tile bounds, in EPSG:3857 meters (minx, miny, maxx, maxy)."""
    R = 6378137.0
    origin = math.pi * R
    n = 2 ** z
    tile_m = 2 * origin / n
    minx = -origin + x * tile_m
    maxx = minx + tile_m
    maxy = origin - y * tile_m
    miny = maxy - tile_m
    return minx, miny, maxx, maxy


def fetch_sentinel_tile(z, x, y):
    """Fetch one Sentinel-2 true-color tile from Sentinel Hub WMS. Cached per (tile, day) so
    ordinary map panning/zooming doesn't burn through the free monthly processing-unit quota -
    the underlying imagery itself only changes on Sentinel-2's own ~5 day revisit anyway."""
    day = date.today().isoformat()
    key = (z, x, y, day)
    if key in _sat_tile_cache:
        return _sat_tile_cache[key]
    secrets = load_secrets()
    instance_id = secrets.get("SH_INSTANCE_ID")
    layer = secrets.get("SH_LAYER_NAME", "TRUE_COLOR")
    if not instance_id:
        raise RuntimeError(
            "SH_INSTANCE_ID missing from secrets.env - create a configuration in the Sentinel Hub "
            "dashboard (shapps.dataspace.copernicus.eu) and see the README for setup steps.")
    token = get_sh_token()
    minx, miny, maxx, maxy = tile_bbox_3857(z, x, y)
    qs = urllib.parse.urlencode({
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": layer, "FORMAT": "image/jpeg", "TRANSPARENT": "false",
        "CRS": "EPSG:3857", "WIDTH": str(SAT_TILE_SIZE), "HEIGHT": str(SAT_TILE_SIZE),
        "BBOX": f"{minx},{miny},{maxx},{maxy}",
        # No TIME param on purpose: the WMS default is "the last valid image", i.e. whatever
        # Sentinel-2 pass is most recently available for this spot (roughly weekly revisit).
    })
    url = f"{SH_WMS_BASE}/{instance_id}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "image/jpeg")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Sentinel Hub WMS request failed ({e.code}): {detail}")
    if len(_sat_tile_cache) >= _SAT_TILE_CACHE_MAX:
        _sat_tile_cache.clear()  # simple reset rather than a full LRU - good enough for a dev server
    _sat_tile_cache[key] = (data, ctype)
    return data, ctype


# ---------- historical wind/cloud data (NOAA NCEI Climate Data Online, GHCN-Daily) ----------
# Needs a free token from https://www.ncdc.noaa.gov/cdo-web/token in secrets.env as NOAA_TOKEN.
# SNOTEL (snow depth/new snow/precip/temp) and the NWS 7-day observations used elsewhere in this
# app are both free/no-key and fetched directly from the browser - this is the one piece that
# needs a server-side proxy, because the CDO API requires a token header and (like the Sentinel
# Hub setup) we never want a secret shipped to the browser.
NOAA_BASE = "https://www.ncei.noaa.gov/cdo-web/api/v2"
_noaa_station_cache = {}   # (round(lat,2), round(lon,2)) -> station dict, or None if nothing found
_noaa_year_cache = {}      # (station_id, year, datatype_key) -> {date: {...partial row...}}
_NOAA_MIN_REQUEST_GAP = 0.22   # seconds - keeps us comfortably under the 5 req/sec token limit

_noaa_last_request_t = [0.0]


def noaa_request(path, params):
    secrets = load_secrets()
    token = secrets.get("NOAA_TOKEN")
    if not token:
        raise LookupError("NOAA_TOKEN missing from secrets.env - get a free token at "
                           "https://www.ncdc.noaa.gov/cdo-web/token and add it there.")
    wait = _NOAA_MIN_REQUEST_GAP - (time.time() - _noaa_last_request_t[0])
    if wait > 0:
        time.sleep(wait)
    qs = urllib.parse.urlencode(params)
    url = f"{NOAA_BASE}/{path}?{qs}"
    req = urllib.request.Request(url, headers={"token": token})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                _noaa_last_request_t[0] = time.time()
                return json.load(r)
        except urllib.error.HTTPError as e:
            _noaa_last_request_t[0] = time.time()
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"NOAA NCEI request failed ({e.code}): {detail}")
    raise RuntimeError("NOAA NCEI request failed: repeated rate-limit (429) responses")


def _haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_nearest_ghcnd_station(lat, lon, datatype_filter=None):
    """Find the nearest GHCN-Daily station with reasonably recent data, expanding the search
    radius until something turns up. Cached per (rounded lat/lon, datatype_filter) so repeated
    clicks near the same spot don't re-query NOAA every time.

    If datatype_filter is given (a comma-joined list of GHCND datatype codes), the /stations
    search is restricted to stations that report at least one of those types - this matters a
    lot in practice: the large majority of GHCND stations in Colorado are basic COOP stations
    that only ever report temperature/precipitation/snow. Wind (and especially cloud cover)
    come from a much sparser set of airport ASOS/AWOS-type stations, so picking the literal
    nearest station (with no filter) very often returns one with no wind/cloud data at all,
    even though the request itself succeeds. The unfiltered radius ladder is kept small since
    a plain temp/precip station is almost always nearby; the filtered one is widened, since a
    wind-reporting station can be considerably farther away, especially in the backcountry."""
    cache_key = (round(lat, 2), round(lon, 2), datatype_filter)
    if cache_key in _noaa_station_cache:
        return _noaa_station_cache[cache_key]
    station = None
    radii = (0.5, 1.0, 2.0, 4.0, 8.0) if datatype_filter else (0.3, 0.75, 1.5, 3.0)
    for radius_deg in radii:
        params = {
            "datasetid": "GHCND",
            "extent": f"{lat - radius_deg},{lon - radius_deg},{lat + radius_deg},{lon + radius_deg}",
            "limit": 1000,
        }
        if datatype_filter:
            params["datatypeid"] = datatype_filter
        j = noaa_request("stations", params)
        results = j.get("results", [])
        today = date.today()
        candidates = []
        for s in results:
            try:
                maxdate = datetime.strptime(s["maxdate"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if (today - maxdate).days > 730:
                continue  # station looks dead - skip it
            d = _haversine_miles(lat, lon, s["latitude"], s["longitude"])
            candidates.append((d, s))
        if candidates:
            candidates.sort(key=lambda t: t[0])
            dist, s = candidates[0]
            station = {"id": s["id"], "name": s.get("name", s["id"]), "lat": s["latitude"],
                       "lon": s["longitude"], "distance_mi": round(dist, 1)}
            break
    _noaa_station_cache[cache_key] = station
    return station


# Used to pick the station (may require a wider search than the plain-nearest one above,
# since wind-reporting stations are sparser than basic temp/precip COOP stations).
_NOAA_WIND_DATATYPES = "AWND,WSFG,WSF5,WSF2"
# Used once a station is picked, to pull every element it might report.
_NOAA_DATATYPES = "AWND,WSFG,WDFG,WSF5,WDF5,WSF2,WDF2,ACMH,ACSH,TMAX,TMIN,TAVG"


def _fetch_ghcnd_year(station_id, year):
    """One calendar year of raw datatype values for a station, as {date: {code: value}}.
    Cached indefinitely - history doesn't change, and GHCND limits daily queries to one
    year per request anyway, so this is also the natural chunk size."""
    cache_key = (station_id, year, _NOAA_DATATYPES)
    if cache_key in _noaa_year_cache:
        return _noaa_year_cache[cache_key]
    out = {}
    offset = 1
    start = f"{year}-01-01"
    end = f"{year}-12-31"
    while True:
        j = noaa_request("data", {
            "datasetid": "GHCND", "stationid": station_id, "datatypeid": _NOAA_DATATYPES,
            "startdate": start, "enddate": end, "units": "standard",
            "limit": 1000, "offset": offset, "sortfield": "date",
        })
        results = j.get("results", [])
        for row in results:
            d = row["date"][:10]
            out.setdefault(d, {})[row["datatype"]] = row["value"]
        if len(results) < 1000:
            break
        offset += 1000
    _noaa_year_cache[cache_key] = out
    return out


def _normalize_ghcnd_day(raw):
    """Map GHCND's many near-duplicate datatype codes down to the single set of fields the
    frontend expects, preferring the most direct/standard code when more than one is present."""
    def pick(*codes):
        for c in codes:
            if c in raw and raw[c] is not None:
                return raw[c]
        return None
    gust = pick("WSFG", "WSF5", "WSF2")
    gust_dir_code = "WDFG" if "WSFG" in raw else ("WDF5" if "WSF5" in raw else "WDF2")
    cloud = pick("ACMH", "ACSH")
    if cloud is not None and cloud > 100:  # defensive: some feeds report tenths of a percent
        cloud = cloud / 10.0
    return {
        "windSpeed": pick("AWND"),
        "windGust": gust,
        "windDirDeg": raw.get(gust_dir_code),
        "cloud": cloud,
        "temp": pick("TAVG"),
        "tempMax": pick("TMAX"),
        "tempMin": pick("TMIN"),
    }


def fetch_noaa_wx(lat, lon, start_date, end_date):
    # Search specifically for a station that reports wind - see find_nearest_ghcnd_station's
    # docstring for why this can't just be "the nearest GHCND station".
    station = find_nearest_ghcnd_station(lat, lon, datatype_filter=_NOAA_WIND_DATATYPES)
    if not station:
        return {"station": None, "data": []}
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])
    rows = []
    for year in range(start_year, end_year + 1):
        year_data = _fetch_ghcnd_year(station["id"], year)
        for d, raw in year_data.items():
            if d < start_date or d > end_date:
                continue
            rec = _normalize_ghcnd_day(raw)
            if all(v is None for v in rec.values()):
                continue
            rec["date"] = d
            rows.append(rec)
    rows.sort(key=lambda r: r["date"])
    return {"station": station, "data": rows}


# ---------- storage ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS zones (
        id TEXT PRIMARY KEY, name TEXT, geometry TEXT);
    CREATE TABLE IF NOT EXISTS snapshots (
        day TEXT, zone_id TEXT, zone_name TEXT,
        danger_level INTEGER, danger_text TEXT,
        above INTEGER, near INTEGER, below INTEGER,
        problems TEXT, bottom_line TEXT, link TEXT,
        fetched_at TEXT,
        PRIMARY KEY (day, zone_id));
    """)
    return con


def save_snapshot(fc, day):
    con = db()
    with con:
        for f in fc["features"]:
            p = f["properties"]
            zid = str(p["id"])
            con.execute("INSERT OR REPLACE INTO zones VALUES (?,?,?)",
                        (zid, p["name"], json.dumps(f["geometry"])))
            b = p.get("bands") or {}
            con.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, zid, p["name"], p.get("danger_level", -1), p.get("danger"),
                 b.get("above"), b.get("near"), b.get("below"),
                 json.dumps(p.get("problems") or []), p.get("bottom_line"),
                 p.get("link"), datetime.utcnow().isoformat()))
    con.close()


def load_day(day):
    """Rebuild a FeatureCollection for a stored day."""
    con = db()
    rows = con.execute(
        "SELECT s.*, z.geometry FROM snapshots s JOIN zones z ON z.id = s.zone_id "
        "WHERE s.day = ?", (day,)).fetchall()
    con.close()
    feats = []
    for r in rows:
        feats.append({
            "type": "Feature",
            "geometry": json.loads(r["geometry"]),
            "properties": {
                "id": r["zone_id"], "name": r["zone_name"],
                "danger_level": r["danger_level"], "danger": r["danger_text"],
                "bands": {"above": r["above"], "near": r["near"], "below": r["below"]},
                "problems": json.loads(r["problems"] or "[]"),
                "bottom_line": r["bottom_line"], "link": r["link"], "day": day,
            }})
    return {"type": "FeatureCollection", "features": feats, "day": day}


# ---------- fetching ----------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bc-mapping/0.1"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def fetch_live():
    layer = get_json(f"{API}/products/map-layer/{CENTER}")
    today = date.today()
    q = urllib.parse.urlencode({
        "type": "forecast", "center_id": CENTER,
        "date_start": (today - timedelta(days=1)).isoformat(),
        "date_end": (today + timedelta(days=1)).isoformat()})
    try:
        products = get_json(f"{API}/products?{q}")
    except Exception as e:  # zones still render without problem details
        print("product fetch failed:", e)
        products = []

    # newest product per forecast zone id
    by_zone = {}
    for prod in products if isinstance(products, list) else []:
        for z in prod.get("forecast_zone") or []:
            key = str(z.get("id"))
            cur = by_zone.get(key)
            if not cur or (prod.get("published_time") or "") > (cur.get("published_time") or ""):
                by_zone[key] = prod

    feats = []
    for f in layer.get("features", []):
        p = f["properties"]
        prod = by_zone.get(str(p.get("id")))
        props = {
            "id": p.get("id"), "name": p.get("name"),
            "danger": p.get("danger"), "danger_level": p.get("danger_level", -1),
            "link": p.get("link"), "travel_advice": p.get("travel_advice"),
            "start_date": p.get("start_date"), "end_date": p.get("end_date"),
            "bands": None, "problems": [], "bottom_line": None,
        }
        if prod:
            props["bottom_line"] = prod.get("bottom_line")
            cur = next((d for d in prod.get("danger") or []
                        if d.get("valid_day") == "current"), None)
            if cur:
                props["bands"] = {"above": cur.get("upper"), "near": cur.get("middle"),
                                  "below": cur.get("lower")}
            for pr in prod.get("forecast_avalanche_problems") or []:
                props["problems"].append({
                    "name": pr.get("name"), "rank": pr.get("rank"),
                    "likelihood": pr.get("likelihood"), "size": pr.get("size"),
                    "location": pr.get("location") or []})
        feats.append({"type": "Feature", "geometry": f["geometry"], "properties": props})
    return {"type": "FeatureCollection", "features": feats, "day": date.today().isoformat()}


def fetch_demo():
    """Rough placeholder boxes with invented ratings, for UI testing only."""
    def box(x, y, w=0.45, h=0.35):
        return {"type": "Polygon", "coordinates": [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]}
    zones = [
        ("Front Range", -105.75, 39.55, 2, (2, 2, 1), [("Wind Slab", ["north upper", "northeast upper", "east upper"])]),
        ("Vail & Summit County", -106.45, 39.35, 3, (3, 3, 2), [("Persistent Slab", ["north upper", "north middle", "northeast upper", "northwest upper"]), ("Wind Slab", ["east upper", "southeast upper"])]),
        ("Steamboat & Flat Tops", -107.05, 40.25, 2, (2, 2, 1), [("Storm Slab", ["north upper", "northwest upper", "west upper"])]),
        ("Sawatch", -106.6, 38.75, 4, (4, 3, 2), [("Deep Persistent Slab", ["north upper", "north middle", "northeast upper", "northeast middle", "east upper"])]),
        ("Aspen", -107.1, 39.0, 3, (3, 3, 2), [("Persistent Slab", ["north upper", "northeast upper", "northwest upper"])]),
        ("Gunnison", -107.05, 38.55, 3, (3, 2, 2), [("Wind Slab", ["northeast upper", "east upper", "southeast upper"])]),
        ("Northern San Juan", -107.95, 37.95, 1, (1, 1, 1), []),
        ("Southern San Juan", -107.4, 37.4, 2, (2, 2, 1), [("Wet Loose", ["south upper", "southeast upper", "southwest upper"])]),
        ("Sangre de Cristo", -105.85, 37.75, 5, (5, 4, 3), [("Storm Slab", ["north upper", "north middle", "northeast upper", "east upper", "northwest upper"])]),
    ]
    names = {1: "low", 2: "moderate", 3: "considerable", 4: "high", 5: "extreme"}
    feats = []
    for i, (n, x, y, lvl, b, probs) in enumerate(zones):
        feats.append({"type": "Feature", "geometry": box(x, y), "properties": {
            "id": 9000 + i, "name": n + " (DEMO)", "danger": names[lvl], "danger_level": lvl,
            "bands": {"above": b[0], "near": b[1], "below": b[2]},
            "problems": [{"name": pn, "rank": k + 1, "likelihood": "Likely", "size": [1, 2],
                          "location": loc} for k, (pn, loc) in enumerate(probs)],
            "bottom_line": "Demo data - not a real forecast.",
            "link": "https://avalanche.state.co.us/"}})
    return {"type": "FeatureCollection", "features": feats,
            "day": date.today().isoformat(), "demo": True}


def get_today(force=False):
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["t"] < CACHE_SECONDS:
            return _cache["data"]
        data = fetch_demo() if DEMO else fetch_live()
        if not DEMO:  # never pollute the archive with demo data
            save_snapshot(data, data["day"])
        _cache.update(t=time.time(), data=data)
        return data


# ---------- http ----------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(HERE, "static"), **kw)

    def end_headers(self):  # never let the browser cache stale pages
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, data, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sat_tile(self, z, x, y):
        try:
            data, ctype = fetch_sentinel_tile(z, x, y)
        except RuntimeError as e:
            # Expected/explained failures: not configured yet, or Sentinel Hub rejected the
            # request. Plain text, not JSON, so the message is readable straight from a browser tab.
            return self._binary(str(e).encode(), "text/plain; charset=utf-8", 503)
        except Exception as e:
            import traceback; traceback.print_exc()
            return self._binary(f"Unexpected error fetching Sentinel-2 tile: {e}".encode(),
                                 "text/plain; charset=utf-8", 502)
        return self._binary(data, ctype)

    def _noaa_wx(self, lat, lon, start, end):
        try:
            result = fetch_noaa_wx(lat, lon, start, end)
        except LookupError as e:
            return self._json({"error": str(e)}, 503)
        except RuntimeError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            import traceback; traceback.print_exc()
            return self._json({"error": f"Unexpected error fetching NOAA data: {e}"}, 502)
        return self._json(result)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        m = SAT_TILE_RE.match(u.path)
        if m:
            z, x, y = (int(v) for v in m.groups())
            return self._sat_tile(z, x, y)
        if u.path == "/api/noaa-wx":
            try:
                lat = float(q["lat"][0]); lon = float(q["lon"][0])
                start = q["start"][0]; end = q["end"][0]
            except (KeyError, ValueError, IndexError):
                return self._json({"error": "lat, lon, start and end query params are required"}, 400)
            return self._noaa_wx(lat, lon, start, end)
        try:
            if u.path == "/api/forecast":
                day = (q.get("date") or [None])[0]
                if not day or day == date.today().isoformat():
                    return self._json(get_today())
                return self._json(load_day(day))
            if u.path == "/api/days":
                con = db()
                days = [r[0] for r in con.execute(
                    "SELECT DISTINCT day FROM snapshots ORDER BY day DESC")]
                con.close()
                return self._json(days)
            if u.path == "/api/history":
                zone = (q.get("zone") or [""])[0]
                con = db()
                rows = con.execute(
                    "SELECT day, danger_level, above, near, below, problems FROM snapshots "
                    "WHERE zone_id = ? ORDER BY day", (zone,)).fetchall()
                con.close()
                return self._json([dict(r) for r in rows])
        except Exception as e:
            import traceback; traceback.print_exc()
            return self._json({"error": str(e)}, 502)
        return super().do_GET()


if __name__ == "__main__":
    print(f"BC Mapping on http://localhost:{PORT}  ({'DEMO data' if DEMO else 'live CAIC data'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
