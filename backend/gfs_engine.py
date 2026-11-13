
#!/usr/bin/env python3
"""SIBILLA GFS V3 - aree terrestri aggregate.
Classi sperimentali, non probabilita' calibrate.
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
    codes_get, codes_get_array,
    codes_grib_new_from_file, codes_release
)
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "forecast"
HOURS = (3, 24, 48, 72)
REGION = (-5.0, 38.0, 30.0, 48.0)
BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
LAND_URL = (
    "https://naturalearth.s3.amazonaws.com/"
    "50m_physical/ne_50m_land.zip"
)
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                obj, f, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")
            )
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def cycles():
    start = NOW.replace(
        hour=(NOW.hour // 6) * 6,
        minute=0, second=0, microsecond=0
    )
    for back in (1, 2, 3):
        yield start - timedelta(hours=back * 6)


def forecast_hour(cycle, horizon):
    elapsed = (NOW - cycle).total_seconds() / 3600
    lead = math.ceil((elapsed + horizon) / 3) * 3
    return max(3, min(120, int(lead)))


def url_for(cycle, lead):
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
        "toplat": str(REGION[3]),
    }
    return BASE + "?" + urllib.parse.urlencode(params)


def download(cycle, lead):
    request = urllib.request.Request(
        url_for(cycle, lead),
        headers={
            "User-Agent": "SIBILLA-Research/3.0",
            "Accept": "application/octet-stream"
        }
    )
    with urllib.request.urlopen(
        request, timeout=75
    ) as response:
        payload = response.read(30_000_001)

    if len(payload) > 30_000_000:
        raise ValueError("GRIB2 troppo grande")
    if not payload.startswith(b"GRIB"):
        raise ValueError("Risposta NOAA non GRIB2")
    return payload


def safe_get(gid, key):
    try:
        return str(codes_get(gid, key))
    except Exception:
        return ""


def identify(gid):
    short = safe_get(gid, "shortName").lower()
    name = safe_get(gid, "name").lower()
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
    inventory = []

    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.seek(0)
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                name = identify(gid)
                level = safe_get(gid, "typeOfLevel")
                inventory.append({
                    "short": safe_get(gid, "shortName"),
                    "level": level
                })
                if name is None:
                    continue
                rank = priority(name, level)
                if name in fields and (
                    fields[name][0] >= rank
                ):
                    continue
                values = codes_get_array(gid, "values")
                lats = codes_get_array(gid, "latitudes")
                lons = codes_get_array(gid, "longitudes")
                if len(values) != len(lats) or (
                    len(values) != len(lons)
                ):
                    continue
                fields[name] = (
                    rank, values, lats, lons
                )
            finally:
                codes_release(gid)

    print("GRIB inventory:", json.dumps(inventory))
    missing = {"cape", "cin", "pwat"} - set(fields)
    if missing:
        raise ValueError(
            "Campi GRIB mancanti: "
            + ", ".join(sorted(missing))
        )
    return fields


def aggregate(fields):
    buckets = defaultdict(
        lambda: {
            "cape": [], "cin": [], "pwat": []
        }
    )
    west, east, south, north = REGION
    for name, (_, values, lats, lons) in fields.items():
        for value, lat, lon in zip(
            values, lats, lons
        ):
            value = float(value)
            lat = float(lat)
            lon = float(lon)
            if lon > 180:
                lon -= 360
            if not (
                west <= lon < east
                and south <= lat < north
                and math.isfinite(value)
                and abs(value) < 1e8
            ):
                continue
            cell = (
                math.floor(lon),
                math.floor(lat)
            )
            buckets[cell][name].append(value)
    return buckets


def grade(cape, cin, pwat):
    # Soglie sperimentali preesistenti.
    if cape < 100 or cin < -250 or pwat < 10:
        return "D"
    if (
        cape >= 2000 and cin >= -75
        and pwat >= 30
    ):
        return "A"
    if (
        cape >= 1000 and cin >= -125
        and pwat >= 22
    ):
        return "B"
    if (
        cape >= 250 and cin >= -200
        and pwat >= 15
    ):
        return "C"
    return "D"


def load_land():
    request = urllib.request.Request(
        LAND_URL,
        headers={"User-Agent": "SIBILLA-Research/3.0"}
    )
    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        content = response.read(12_000_001)

    if len(content) > 12_000_000:
        raise ValueError("Natural Earth troppo grande")

    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "land.zip"
        archive.write_bytes(content)
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            required = (
                "ne_50m_land.shp",
                "ne_50m_land.shx",
                "ne_50m_land.dbf"
            )
            if not all(n in names for n in required):
                raise ValueError(
                    "Shapefile Natural Earth incompleto"
                )
            for name in required:
                (Path(directory) / name).write_bytes(
                    z.read(name)
                )

        reader = shapefile.Reader(
            str(Path(directory) / "ne_50m_land.shp")
        )
        extent = box(*REGION)
        pieces = []
        for record in reader.shapes():
            geom = shape(record.__geo_interface__)
            if geom.intersects(extent):
                pieces.append(
                    geom.intersection(extent)
                )
        reader.close()

    if not pieces:
        raise ValueError("Maschera terrestre vuota")
    land = unary_union(pieces)
    if land.is_empty:
        raise ValueError("Maschera terrestre non valida")
    return land


def polygons(geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type in (
        "MultiPolygon", "GeometryCollection"
    ):
        for part in geometry.geoms:
            yield from polygons(part)


def geojson(fields, cycle, lead, horizon, land):
    buckets = aggregate(fields)
    by_class = defaultdict(list)
    stats = defaultdict(list)
    cell_count = 0

    for (x, y), values in sorted(buckets.items()):
        if not all(
            values[k] for k in (
                "cape", "cin", "pwat"
            )
        ):
            continue
        cape = max(values["cape"])
        cin = max(values["cin"])
        pwat = (
            sum(values["pwat"]) /
            len(values["pwat"])
        )
        category = grade(cape, cin, pwat)
        if category == "D":
            continue

        square = box(x, y, x + 1, y + 1)
        if not square.intersects(land):
            continue

        by_class[category].append(square)
        stats[category].append(
            (cape, cin, pwat)
        )
        cell_count += 1

    features = []
    for category in ("A", "B", "C"):
        squares = by_class[category]
        if not squares:
            continue

        merged = unary_union(squares)
        clipped = merged.intersection(land)
        if not clipped.is_valid:
            clipped = clipped.buffer(0)

        for part in polygons(clipped):
            if part.is_empty or part.area <= 0:
                continue
            anchor = part.representative_point()
            samples = stats[category]
            n = len(samples)
            features.append({
                "type": "Feature",
                "geometry": mapping(part),
                "properties": {
                    "class": category,
                    "category": category,
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
            "status": "ready",
            "model": "NOAA GFS 0.25 degree",
            "index_type": (
                "experimental_convective_potential"
            ),
            "forecast_available": True,
            "probability_calibrated": False,
            "horizon_hours": horizon,
            "model_cycle": iso(cycle),
            "forecast_valid_at": iso(
                cycle + timedelta(hours=lead)
            ),
            "generated_at": iso(NOW),
            "source": "NOAA NOMADS GFS GRIB2",
            "land_source": (
                "Natural Earth 1:50m land"
            ),
            "land_mask": True,
            "geometry_type": (
                "merged_land_clipped_polygons"
            ),
            "geometry_resolution_degrees": 1,
            "input_land_cells": cell_count,
            "note": (
                "Indice euristico non calibrato; "
                "maschera geografica non aumenta "
                "la risoluzione meteorologica"
            )
        }
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    land = load_land()
    print("Natural Earth land mask: OK")

    for horizon in HOURS:
        errors = []
        for cycle in cycles():
            lead = forecast_hour(cycle, horizon)
            try:
                payload = download(cycle, lead)
                fields = decode(payload)
                product = geojson(
                    fields, cycle, lead,
                    horizon, land
                )
                atomic_json(
                    OUT / f"{horizon}h.geojson",
                    product
                )
                results[str(horizon)] = {
                    "status": "ready",
                    "features": len(
                        product["features"]
                    ),
                    "land_cells": product[
                        "metadata"
                    ]["input_land_cells"],
                    "valid_at": product[
                        "metadata"
                    ]["forecast_valid_at"]
                }
                print(
                    f"GFS +{horizon}h: "
                    f"{len(product['features'])} "
                    "aree terrestri"
                )
                break
            except Exception as exc:
                message = (
                    f"{iso(cycle)} "
                    f"{type(exc).__name__}: {exc}"
                )
                errors.append(message)
                print(
                    f"GFS +{horizon}h: {message}"
                )
        else:
            results[str(horizon)] = {
                "status": "unavailable",
                "errors": errors
            }

    atomic_json(
        DATA / "gfs_status.json",
        {
            "generated_at": iso(NOW),
            "engine": "experimental_gfs_v3_land",
            "horizons": results
        }
    )
    if not any(
        item["status"] == "ready"
        for item in results.values()
    ):
        raise RuntimeError(
            "Nessuna scadenza GFS disponibile"
        )


if __name__ == "__main__":
    main()
    
