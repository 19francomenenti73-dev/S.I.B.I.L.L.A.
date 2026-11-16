#!/usr/bin/env python3
"""SIBILLA ECMWF IFS V1.
Dati ufficiali ECMWF Open Data CC BY 4.0.
MUCAPE + TCWV, senza CIN sintetico.
Orizzonti nominali: 4, 8, 16, 24, 36 ore.
"""
import json
import math
import os
import tempfile
import zipfile
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from io import BytesIO

import shapefile
from ecmwf.opendata import Client
from eccodes import (
    codes_grib_new_from_file,
    codes_get,
    codes_get_array,
    codes_release
)
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "models" / "ecmwf"
STATUS = ROOT / "data" / "ecmwf_status.json"
HOURS = (4, 8, 16, 24, 36)
REGION = (-5.0, 38.0, 30.0, 48.0)
LAND_URL = (
    "https://naturalearth.s3.amazonaws.com/"
    "50m_physical/ne_50m_land.zip"
)
SOURCE = "ECMWF IFS Open Data 0.25 degree"
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
                allow_nan=False,
                separators=(",", ":")
            )
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def cycles():
    base = NOW.replace(
        hour=(NOW.hour // 6) * 6,
        minute=0, second=0, microsecond=0
    )
    # Si evitano cicli troppo recenti.
    for back in (2, 3, 4):
        yield base - timedelta(hours=6 * back)


def lead_hour(cycle, horizon):
    elapsed = (NOW - cycle).total_seconds() / 3600
    lead = math.ceil((elapsed + horizon) / 3) * 3
    return max(3, int(lead))


def download(client, cycle, lead, path):
    client.retrieve(
        date=cycle.strftime("%Y%m%d"),
        time=cycle.hour,
        stream="oper",
        type="fc",
        levtype="sfc",
        step=lead,
        param=["mucape", "tcwv"],
        target=str(path)
    )
    if not path.exists():
        raise ValueError("Download ECMWF assente")
    if path.stat().st_size > 80_000_000:
        raise ValueError("GRIB troppo grande")
    with path.open("rb") as f:
        if f.read(4) != b"GRIB":
            raise ValueError("File ECMWF non GRIB")


def decode(path):
    fields = {}
    with path.open("rb") as stream:
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                param = str(
                    codes_get(gid, "shortName")
                ).lower()
                if param not in ("mucape", "tcwv"):
                    continue
                if param in fields:
                    raise ValueError(
                        "Parametro duplicato: " + param
                    )
                values = codes_get_array(gid, "values")
                lats = codes_get_array(gid, "latitudes")
                lons = codes_get_array(gid, "longitudes")
                if not (
                    len(values) == len(lats) == len(lons)
                ):
                    raise ValueError("Griglia incoerente")
                fields[param] = (values, lats, lons)
            finally:
                codes_release(gid)

    if set(fields) != {"mucape", "tcwv"}:
        raise ValueError(
            "Campi ECMWF mancanti: " +
            str(set(("mucape", "tcwv")) - set(fields))
        )
    return fields


def aggregate(fields):
    buckets = defaultdict(
        lambda: {"mucape": [], "tcwv": []}
    )
    west, east, south, north = REGION
    for name, (values, lats, lons) in fields.items():
        for v, lat, lon in zip(values, lats, lons):
            v, lat, lon = float(v), float(lat), float(lon)
            if lon > 180:
                lon -= 360
            if not (
                west <= lon < east
                and south <= lat < north
                and math.isfinite(v)
                and 0 <= v < 1e7
            ):
                continue
            key = (math.floor(lon), math.floor(lat))
            buckets[key][name].append(v)
    return buckets


def grade(cape, water):
    """Classi euristiche ECMWF, non calibrate.

    Non equivalgono automaticamente alle
    classi GFS: manca il contributo del CIN.
    """
    if cape >= 2000 and water >= 30:
        return "A"
    if cape >= 1000 and water >= 22:
        return "B"
    if cape >= 250 and water >= 15:
        return "C"
    return None


def land_mask():
    request = urllib.request.Request(
        LAND_URL,
        headers={"User-Agent": "SIBILLA-Research/1.0"}
    )
    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        payload = response.read(12_000_001)

    if len(payload) > 12_000_000:
        raise ValueError("Maschera troppo grande")

    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        with zipfile.ZipFile(BytesIO(payload)) as z:
            for ext in ("shp", "shx", "dbf"):
                name = f"ne_50m_land.{ext}"
                (folder / name).write_bytes(z.read(name))

        reader = shapefile.Reader(
            str(folder / "ne_50m_land.shp")
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
        if not values["mucape"] or not values["tcwv"]:
            continue
        cape = max(values["mucape"])
        water = sum(values["tcwv"]) / len(values["tcwv"])
        category = grade(cape, water)
        if category is None:
            continue
        square = box(x, y, x + 1, y + 1)
        if not square.intersects(land):
            continue
        by_class[category].append(square)
        stats[category].append((cape, water))

    features = []
    for category in ("A", "B", "C"):
        squares = by_class[category]
        if not squares:
            continue
        merged = unary_union(squares).intersection(land)
        if not merged.is_valid:
            merged = merged.buffer(0)
        samples = stats[category]
        n = len(samples)

        for part in polygons(merged):
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
                    "cin_mean_j_kg": None,
                    "pwat_mean_kg_m2": round(
                        sum(v[1] for v in samples) / n, 1
                    ),
                    "observed": False,
                    "probability_calibrated": False
                }
            })

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "product": "SIBILLA",
            "model_id": "ecmwf",
            "model": SOURCE,
            "status": "ready",
            "horizon_hours": horizon,
            "model_cycle": iso(cycle),
            "forecast_valid_at": iso(
                cycle + timedelta(hours=lead)
            ),
            "generated_at": iso(
                datetime.now(timezone.utc)
            ),
            "requested_horizon_hours": horizon,
            "effective_horizon_hours": round(
                (cycle + timedelta(hours=lead) -
                 NOW).total_seconds() / 3600, 2
            ),
            "source": SOURCE,
            "source_license": "CC BY 4.0",
            "source_url": (
                "https://www.ecmwf.int/en/"
                "forecasts/datasets/open-data"
            ),
            "parameters": ["mucape", "tcwv"],
            "cin_available": False,
            "index_type": "experimental_ecmwf",
            "land_source": "Natural Earth 1:50m",
            "land_mask": True,
            "geometry_type":
                "merged_land_clipped_polygons",
            "geometry_resolution_degrees": 1,
            "probability_calibrated": False,
            "forecast_available": True,
            "note": (
                "Indice sperimentale MUCAPE/TCWV. "
                "CIN non disponibile. "
                "Classi non calibrate e non "
                "direttamente equivalenti a GFS."
            )
        }
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    client = Client(
        source="ecmwf",
        model="ifs",
        resol="0p25"
    )
    land = land_mask()
    results = {}

    for horizon in HOURS:
        errors = []
        for cycle in cycles():
            lead = lead_hour(cycle, horizon)
            try:
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "ecmwf.grib2"
                    download(client, cycle, lead, path)
                    fields = decode(path)

                product = build(
                    fields, cycle, lead, horizon, land
                )
                atomic_json(
                    OUT / f"{horizon}h.geojson",
                    product
                )
                results[str(horizon)] = {
                    "status": "ready",
                    "features": len(product["features"]),
                    "model_cycle": iso(cycle),
                    "valid_at": product["metadata"][
                        "forecast_valid_at"
                    ]
                }
                print(f"ECMWF +{horizon}h: OK")
                break
            except Exception as exc:
                errors.append(
                    f"{iso(cycle)}: {type(exc).__name__}: {exc}"
                )
        else:
            stale = OUT / f"{horizon}h.geojson"
            if stale.exists():
                stale.unlink()
            results[str(horizon)] = {
                "status": "unavailable",
                "errors": errors
            }

    atomic_json(STATUS, {
        "generated_at": iso(
            datetime.now(timezone.utc)
        ),
        "engine": "ecmwf_ifs_v1",
        "horizons": results
    })

    if not any(
        r["status"] == "ready"
        for r in results.values()
    ):
        raise RuntimeError(
            "Nessuna previsione ECMWF disponibile"
        )


if __name__ == "__main__":
    main()
          
