
#!/usr/bin/env python3
"""SIBILLA - GFS Engine V4.
NOAA/NCEP GFS 0.25 gradi.
Orizzonti: 4, 8, 16, 24, 36 ore.
Classi sperimentali A/B/C.
Solo aree terrestri.
"""

import json
import math
import os
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import shapefile
from eccodes import (
    codes_get,
    codes_get_array,
    codes_grib_new_from_file,
    codes_release
)
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "models" / "gfs"
HOURS = (4, 8, 16, 24, 36)
REGION = (-5.0, 38.0, 30.0, 48.0)
BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
LAND_URL = (
    "https://naturalearth.s3.amazonaws.com/"
    "50m_physical/ne_50m_land.zip"
)
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data, f, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")
            )
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cycles():
    latest = NOW.replace(
        hour=(NOW.hour // 6) * 6,
        minute=0, second=0, microsecond=0
    )
    for back in (1, 2, 3, 4):
        yield latest - timedelta(hours=6 * back)


def lead_hour(cycle, horizon):
    elapsed = (NOW - cycle).total_seconds() / 3600
    return max(3, math.ceil(
        (elapsed + horizon) / 3
    ) * 3)


def download(cycle, lead):
    params = {
        "file": (
            f"gfs.t{cycle:%H}z."
            f"pgrb2.0p25.f{lead:03d}"
        ),
        "dir": (
            f"/gfs.{cycle:%Y%m%d}/"
            f"{cycle:%H}/atmos"
        ),
        "var_CAPE": "on",
        "var_CIN": "on",
        "var_PWAT": "on",
        "all_lev": "on",
        "subregion": "",
        "leftlon": str(REGION[0]),
        "rightlon": str(REGION[1]),
        "bottomlat": str(REGION[2]),
        "toplat": str(REGION[3])
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SIBILLA-Research/4.0"}
    )
    with urllib.request.urlopen(
        request, timeout=75
    ) as response:
        payload = response.read(30_000_001)

    if len(payload) > 30_000_000:
        raise ValueError("GRIB troppo grande")
    if not payload.startswith(b"GRIB"):
        raise ValueError("Risposta NOAA non GRIB")
    return payload


def get(gid, key):
    try:
        return str(codes_get(gid, key))
    except Exception:
        return ""


def identify(gid):
    short = get(gid, "shortName").lower()
    name = get(gid, "name").lower()
    if short == "cape" or (
        "convective available potential" in name
    ):
        return "cape"
    if short in ("cin", "cins") or (
        "convective inhibition" in name
    ):
        return "cin"
    if short == "pwat" or (
        "precipitable water" in name
    ):
        return "pwat"
    return None


def priority(name, level):
    level = level.lower()
    if name == "pwat":
        return 4 if "entireatmosphere" in level else 1
    if "surface" in level:
        return 4
    if "pressurefromgroundlayer" in level:
        return 3
    if "heightabovegroundlayer" in level:
        return 2
    return 1


def decode(payload):
    fields = {}
    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.seek(0)
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                name = identify(gid)
                if name is None:
                    continue
                level = get(gid, "typeOfLevel")
                rank = priority(name, level)
                if name in fields and fields[name][0] >= rank:
                    continue
                values = codes_get_array(gid, "values")
                lats = codes_get_array(gid, "latitudes")
                lons = codes_get_array(gid, "longitudes")
                if not (
                    len(values) == len(lats) == len(lons)
                ):
                    continue
                fields[name] = (rank, values, lats, lons)
            finally:
                codes_release(gid)

    missing = {"cape", "cin", "pwat"} - set(fields)
    if missing:
        raise ValueError(
            "Campi mancanti: " + ", ".join(sorted(missing))
        )
    return fields


def aggregate(fields):
    buckets = defaultdict(
        lambda: {"cape": [], "cin": [], "pwat": []}
    )
    west, east, south, north = REGION
    for name, (_, values, lats, lons) in fields.items():
        for value, lat, lon in zip(values, lats, lons):
            value, lat, lon = float(value), float(lat), float(lon)
            if lon > 180:
                lon -= 360
            if not (
                west <= lon < east
                and south <= lat < north
                and math.isfinite(value)
                and abs(value) < 1e8
            ):
                continue
            cell = (math.floor(lon), math.floor(lat))
            buckets[cell][name].append(value)
    return buckets


def grade(cape, cin, pwat):
    if cape < 100 or cin < -250 or pwat < 10:
        return None
    if cape >= 2000 and cin >= -75 and pwat >= 30:
        return "A"
    if cape >= 1000 and cin >= -125 and pwat >= 22:
        return "B"
    if cape >= 250 and cin >= -200 and pwat >= 15:
        return "C"
    return None


def load_land():
    request = urllib.request.Request(
        LAND_URL,
        headers={"User-Agent": "SIBILLA-Research/4.0"}
    )
    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        content = response.read(12_000_001)

    if len(content) > 12_000_000:
        raise ValueError("Natural Earth troppo grande")

    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        with zipfile.ZipFile(
            __import__("io").BytesIO(content)
        ) as archive:
            for suffix in ("shp", "shx", "dbf"):
                name = f"ne_50m_land.{suffix}"
                (directory / name).write_bytes(archive.read(name))

        reader = shapefile.Reader(
            str(directory / "ne_50m_land.shp")
        )
        extent = box(*REGION)
        pieces = []
        for record in reader.shapes():
            geom = shape(record.__geo_interface__)
            if geom.intersects(extent):
                pieces.append(geom.intersection(extent))
        reader.close()

    if not pieces:
        raise ValueError("Maschera terrestre vuota")
    return unary_union(pieces)


def polygons(geom):
    if geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in (
        "MultiPolygon", "GeometryCollection"
    ):
        for part in geom.geoms:
            yield from polygons(part)


def build(fields, cycle, lead, horizon, land):
    buckets = aggregate(fields)
    by_class = defaultdict(list)
    stats = defaultdict(list)

    for (x, y), values in sorted(buckets.items()):
        if not all(values[k] for k in (
            "cape", "cin", "pwat"
        )):
            continue

        cape = max(values["cape"])
        cin = max(values["cin"])
        pwat = sum(values["pwat"]) / len(values["pwat"])
        category = grade(cape, cin, pwat)
        if category is None:
            continue

        square = box(x, y, x + 1, y + 1)
        if not square.intersects(land):
            continue

        by_class[category].append(square)
        stats[category].append((cape, cin, pwat))

    features = []
    for category in ("A", "B", "C"):
        squares = by_class[category]
        if not squares:
            continue

        clipped = unary_union(squares).intersection(land)
        if not clipped.is_valid:
            clipped = clipped.buffer(0)

        samples = stats[category]
        n = len(samples)

        for part in polygons(clipped):
            if part.is_empty or part.area <= 0:
                continue
            anchor = part.representative_point()
            features.append({
                "type": "Feature",
                "geometry": mapping(part),
                "properties": {
                    "class": category,
                    "label_lon": round(anchor.x, 5),
                    "label_lat": round(anchor.y, 5),
                    "cape_mean_j_kg": round(
                        sum(v[0] for v in samples) / n
                    ),
                    "cin_mean_j_kg": round(
                        sum(v[1] for v in samples) / n
                    ),
                    "pwat_mean_kg_m2": round(
                        sum(v[2] for v in samples) / n, 1
                    ),
                    "probability_calibrated": False,
                    "observed": False
                }
            })

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "product": "SIBILLA",
            "model_id": "gfs",
            "model": "NOAA GFS 0.25 degree",
            "status": "ready",
            "horizon_hours": horizon,
            "model_cycle": iso(cycle),
            "forecast_valid_at": iso(
                cycle + timedelta(hours=lead)
            ),
            "generated_at": iso(NOW),
            "source": "NOAA/NCEP NOMADS GFS",
            "land_source": "Natural Earth 1:50m",
            "land_mask": True,
            "geometry_type": "merged_land_clipped_polygons",
            "geometry_resolution_degrees": 1,
            "probability_calibrated": False,
            "forecast_available": True,
            "note": (
                "Indice euristico sperimentale. "
                "Non rappresenta probabilita' calibrate. "
                "Le statistiche sono aggregate per classe."
            )
        }
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    land = load_land()
    results = {}

    for horizon in HOURS:
        errors = []
        for cycle in cycles():
            lead = lead_hour(cycle, horizon)
            try:
                payload = download(cycle, lead)
                fields = decode(payload)
                product = build(
                    fields, cycle, lead, horizon, land
                )
                save_json(
                    OUT / f"{horizon}h.geojson", product
                )
                results[str(horizon)] = {
                    "status": "ready",
                    "features": len(product["features"]),
                    "valid_at": product["metadata"][
                        "forecast_valid_at"
                    ]
                }
                print(f"GFS +{horizon}h: OK")
                break
            except Exception as exc:
                errors.append(str(exc))
        else:
            results[str(horizon)] = {
                "status": "unavailable",
                "errors": errors
            }
            stale = OUT / f"{horizon}h.geojson"
            if stale.exists():
                stale.unlink()

    save_json(
        ROOT / "data" / "gfs_status.json",
        {
            "generated_at": iso(NOW),
            "engine": "gfs_v4",
            "horizons": results
        }
    )

    if not any(
        v["status"] == "ready"
        for v in results.values()
    ):
        raise RuntimeError(
            "Nessuna previsione GFS disponibile"
        )


if __name__ == "__main__":
    main()
    
