
#!/usr/bin/env python3
"""S.I.B.I.L.L.A. - ICON-2I ItaliaMeteo V2."""
import bz2
import io
import json
import math
import os
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import shapefile
from eccodes import (
    codes_get, codes_get_array,
    codes_grib_new_from_file, codes_release
)
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
BASE = ("https://meteohub.agenziaitaliameteo.it"
        "/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS/")
OUT = ROOT / "data/models/icon"
STATUS = ROOT / "data/icon_status.json"
HOURS = (4, 8, 16, 24, 36)
PARAMS = ("CAPE_ML", "CIN_ML", "TQV")
REGION = (-5.0, 30.0, 38.0, 48.0)
RESOLUTION = 0.25
MAX_BYTES = 250_000_000

CREDIT = {
    "provider": "Agenzia ItaliaMeteo",
    "dataset": "ICON-2I",
    "license": "CC BY 4.0",
    "license_url":
        "https://creativecommons.org/licenses/by/4.0/",
    "source_url": BASE,
    "modified": True,
    "modifications":
        "Aggregazione spaziale, classificazione "
        "sperimentale e ritaglio terrestre"
}

def now():
    return datetime.now(timezone.utc)

def iso(t):
    return t.isoformat(timespec="seconds").replace(
        "+00:00", "Z")

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.items.append(value)

def checked_url(url):
    p = urllib.parse.urlparse(url)
    if (p.scheme != "https" or
        p.hostname !=
            "meteohub.agenziaitaliameteo.it" or
        not p.path.startswith(
            "/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS/")):
        raise ValueError("URL non autorizzato")
    return urllib.request.Request(
        url, headers={"User-Agent": "SIBILLA/2.0"})

def links(url):
    with urllib.request.urlopen(
        checked_url(url), timeout=35
    ) as response:
        content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError("Indice troppo grande")
    parser = Links()
    parser.feed(content.decode("utf-8", "replace"))
    return [
        urllib.parse.urljoin(url, x)
        for x in parser.items
        if urllib.parse.urljoin(url, x).startswith(BASE)
    ]

def cycles():
    found = []
    for url in links(BASE):
        name = url.rstrip("/").split("/")[-1]
        if re.fullmatch(r"\d{10}", name):
            dt = datetime.strptime(
                name, "%Y%m%d%H"
            ).replace(tzinfo=timezone.utc)
            if dt <= now():
                found.append((dt, url))
    return sorted(found, reverse=True)

def variable_file(cycle_url, param):
    folder = cycle_url.rstrip("/") + "/" + param + "/"
    candidates = [
        u for u in links(folder)
        if u.lower().endswith(
            (".grib", ".grib2", ".grb",
             ".grib.bz2", ".grib2.bz2")
        )
    ]
    if not candidates:
        raise FileNotFoundError(
            f"Nessun GRIB per {param}")
    # Preferisce il prodotto atmosferico ML
    # senza supporre scadenze dal nome.
    return sorted(candidates)[0]

def download(url, path):
    total = 0
    with urllib.request.urlopen(
        checked_url(url), timeout=90
    ) as response:
        with path.open("wb") as f:
            while True:
                part = response.read(1024 * 1024)
                if not part:
                    break
                total += len(part)
                if total > MAX_BYTES:
                    raise ValueError(
                        "Download oltre limite")
                f.write(part)
    if not total:
        raise ValueError("Download vuoto")
    return total

def unpack(path, folder):
    with path.open("rb") as f:
        magic = f.read(3)
    if magic != b"BZh":
        return path
    output = folder / "unpacked.grib"
    with bz2.open(path, "rb") as src:
        with output.open("wb") as dst:
            while True:
                part = src.read(1024 * 1024)
                if not part:
                    break
                dst.write(part)
    return output

def get(gid, key):
    try:
        return codes_get(gid, key)
    except Exception:
        return None

def valid_time(gid):
    date = get(gid, "validityDate")
    hour = get(gid, "validityTime")
    if date is None or hour is None:
        raise ValueError(
            "Validità GRIB non disponibile")
    return datetime.strptime(
        f"{int(date):08d}{int(hour):04d}",
        "%Y%m%d%H%M"
    ).replace(tzinfo=timezone.utc)

def decode(path, expected, cycle):
    results = {}
    count = 0
    with path.open("rb") as stream:
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                count += 1
                short = str(
                    get(gid, "shortName") or ""
                ).upper()
                if short != expected:
                    continue
                date = int(get(gid, "dataDate"))
                hour = int(get(gid, "dataTime"))
                if (date != int(cycle.strftime("%Y%m%d"))
                    or hour != cycle.hour * 100):
                    continue
                vt = valid_time(gid)
                if vt <= now():
                    continue
                if get(gid, "gridType") != "regular_ll":
                    raise ValueError(
                        "Griglia non geografica regolare")
                values = np.asarray(
                    codes_get_array(gid, "values"),
                    dtype=np.float64
                )
                lat = np.asarray(
                    codes_get_array(gid, "latitudes"),
                    dtype=np.float64
                )
                lon = np.asarray(
                    codes_get_array(gid, "longitudes"),
                    dtype=np.float64
                )
                if not (
                    len(values) == len(lat) == len(lon)
                ):
                    raise ValueError(
                        "Dimensioni GRIB incoerenti")
                results[vt] = (values, lat, lon)
            finally:
                codes_release(gid)
    print(expected, "messages:", count,
          "valid times:", len(results), flush=True)
    if not results:
        raise ValueError(
            f"Nessun messaggio valido per {expected}")
    return results

def select_times(fields):
    common = set.intersection(
        *(set(fields[p]) for p in PARAMS)
    )
    chosen = {}
    reference = now()
    for h in HOURS:
        target = reference + timedelta(hours=h)
        candidates = [
            t for t in common
            if abs((t-target).total_seconds()) <= 3600
        ]
        if candidates:
            chosen[h] = min(
                candidates,
                key=lambda t: abs(
                    (t-target).total_seconds())
            )
    return chosen
    
def land_mask():
    url = (
        "https://naturalearth.s3.amazonaws.com/"
        "50m_physical/ne_50m_land.zip"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "SIBILLA/2.0"}
    )
    with urllib.request.urlopen(
        req, timeout=45
    ) as response:
        raw = response.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError("Maschera troppo grande")
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for ext in ("shp", "shx", "dbf"):
                filename = f"ne_50m_land.{ext}"
                (folder / filename).write_bytes(
                    z.read(filename))
        reader = shapefile.Reader(
            str(folder / "ne_50m_land.shp"))
        extent = box(
            REGION[0], REGION[2],
            REGION[1], REGION[3])
        pieces = []
        for item in reader.shapes():
            geom = shape(item.__geo_interface__)
            if geom.intersects(extent):
                pieces.append(
                    geom.intersection(extent))
        reader.close()
    if not pieces:
        raise RuntimeError(
            "Maschera terrestre vuota")
    return unary_union(pieces)

def classify(cape, cin, tqv):
    if not all(math.isfinite(x)
               for x in (cape, cin, tqv)):
        return None
    if cape >= 2000 and cin >= -75 and tqv >= 30:
        return "A"
    if cape >= 1000 and cin >= -125 and tqv >= 22:
        return "B"
    if cape >= 250 and cin >= -200 and tqv >= 15:
        return "C"
    return None

def aggregate(fields, vt):
    arrays = [fields[p][vt] for p in PARAMS]
    base_lat = arrays[0][1]
    base_lon = arrays[0][2]
    for values, lat, lon in arrays[1:]:
        if len(values) != len(arrays[0][0]):
            raise ValueError(
                "Dimensioni parametri differenti")
        if not (
            np.array_equal(lat, base_lat) and
            np.array_equal(lon, base_lon)
        ):
            raise ValueError(
                "Griglie parametri non coincidenti")

    lon = ((base_lon + 180) % 360) - 180
    lat = base_lat
    mask = (
        (lon >= REGION[0]) &
        (lon < REGION[1]) &
        (lat >= REGION[2]) &
        (lat < REGION[3])
    )
    for values, _, _ in arrays:
        mask &= (
            np.isfinite(values) &
            (np.abs(values) < 1e8)
        )
    x = np.floor(
        (lon[mask] - REGION[0]) / RESOLUTION
    ).astype(np.int32)
    y = np.floor(
        (lat[mask] - REGION[2]) / RESOLUTION
    ).astype(np.int32)
    nx = int(round(
        (REGION[1]-REGION[0])/RESOLUTION))
    ids = y * nx + x
    counts = np.bincount(ids)
    means = []
    for values, _, _ in arrays:
        sums = np.bincount(
            ids, weights=values[mask],
            minlength=len(counts))
        means.append(
            np.divide(
                sums, counts,
                out=np.zeros_like(sums),
                where=counts > 0
            )
        )
    return counts, means, nx

def make_product(fields, vt, horizon, cycle, land):
    counts, means, nx = aggregate(fields, vt)
    groups = defaultdict(list)
    for idx in np.flatnonzero(counts):
        cape, cin, tqv = (
            float(m[idx]) for m in means
        )
        category = classify(cape, cin, tqv)
        if category is None:
            continue
        x = int(idx % nx)
        y = int(idx // nx)
        west = REGION[0] + x * RESOLUTION
        south = REGION[2] + y * RESOLUTION
        groups[category].append(
            box(west, south,
                west + RESOLUTION,
                south + RESOLUTION)
        )
    features = []
    for category in ("A", "B", "C"):
        cells = groups[category]
        if not cells:
            continue
        geom = unary_union(cells).intersection(land)
        if geom.is_empty:
            continue
        if geom.geom_type == "GeometryCollection":
            polys = [
                g for g in geom.geoms
                if g.geom_type in (
                    "Polygon", "MultiPolygon")
            ]
            if not polys:
                continue
            geom = unary_union(polys)
        features.append({
            "type": "Feature",
            "properties": {
                "class": category,
                "model": "ICON-2I",
                "experimental": True
            },
            "geometry": mapping(geom)
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "product": "SIBILLA",
            "model_id": "icon",
            "model_name": "ICON-2I",
            "status": "ready",
            "horizon_hours": horizon,
            "model_cycle": iso(cycle),
            "forecast_valid_at": iso(vt),
            "generated_at": iso(now()),
            "land_mask": True,
            "geometry_type":
                "merged_land_clipped_polygons",
            "geometry_resolution_degrees": RESOLUTION,
            "probability_calibrated": False,
            "classification_method":
                "experimental_CIN_CAPE_TQV_thresholds",
            "source": CREDIT
        }
    }

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "model": "ICON-2I",
        "generated_at": iso(now()),
        "status": "checking",
        "forecast_ready": False,
        "source": CREDIT,
        "horizons": {}
    }
    try:
        candidates = cycles()
        if not candidates:
            raise RuntimeError(
                "Nessun ciclo ufficiale disponibile")
        fields = None
        cycle = None
        urls = None

        # Cerca un ciclo con tutte le variabili.
        for dt, folder in candidates[:3]:
            try:
                found = {
                    p: variable_file(folder, p)
                    for p in PARAMS
                }
                cycle, urls = dt, found
                break
            except Exception as exc:
                print("Cycle skipped:", dt, exc)

        if urls is None:
            raise RuntimeError(
                "Nessun ciclo con tre parametri")

        report["cycle"] = iso(cycle)
        report["files"] = urls
        fields = {}

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for param in PARAMS:
                raw = folder / f"{param}.download"
                print("Downloading:", param, flush=True)
                size = download(urls[param], raw)
                print("Bytes:", size, flush=True)
                decoded = unpack(raw, folder)
                fields[param] = decode(
                    decoded, param, cycle)
                raw.unlink(missing_ok=True)
                if decoded != raw:
                    decoded.unlink(missing_ok=True)

        selected = select_times(fields)
        report["available_horizons"] = sorted(
            selected.keys())

        if not selected:
            raise RuntimeError(
                "Nessuna scadenza comune disponibile")

        land = land_mask()

        for horizon in HOURS:
            path = OUT / f"{horizon}h.geojson"
            if horizon not in selected:
                path.unlink(missing_ok=True)
                report["horizons"][str(horizon)] = {
                    "status": "unavailable"
                }
                continue
            try:
                vt = selected[horizon]
                product = make_product(
                    fields, vt, horizon, cycle, land)
                save(path, product)
                report["horizons"][str(horizon)] = {
                    "status": "ready",
                    "valid_at": iso(vt),
                    "features": len(product["features"])
                }
                print(
                    f"ICON-2I +{horizon}h ready",
                    flush=True)
            except Exception as exc:
                path.unlink(missing_ok=True)
                report["horizons"][str(horizon)] = {
                    "status": "error",
                    "error": str(exc)
                }

        ready = sum(
            x["status"] == "ready"
            for x in report["horizons"].values()
        )
        report["ready_count"] = ready
        report["forecast_ready"] = ready > 0
        report["status"] = (
            "ready" if ready == len(HOURS)
            else "partial" if ready else "failed"
        )

    except Exception as exc:
        report["status"] = "failed"
        report["error"] = (
            type(exc).__name__ + ": " + str(exc))
        for h in HOURS:
            (OUT / f"{h}h.geojson").unlink(
                missing_ok=True)
    finally:
        save(STATUS, report)
        print(json.dumps(
            report, indent=2,
            ensure_ascii=False), flush=True)

    if not report["forecast_ready"]:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
            
