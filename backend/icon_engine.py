#!/usr/bin/env python3
"""SIBILLA - ICON-EU Engine V1.
DWD Open Data, griglia ICON nativa.
CAPE_ML, CIN_ML, TQV.
Classi sperimentali A/B/C.
"""
import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import shapefile
from dwdopen import DWD
from eccodes import (
    codes_grib_new_from_file,
    codes_get,
    codes_get_array,
    codes_release
)
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "models" / "icon"
STATUS = ROOT / "data" / "icon_status.json"
HOURS = (4, 8, 16, 24, 36)
REGION = (-5.0, 38.0, 30.0, 48.0)
PARAMS = ("CAPE_ML", "CIN_ML", "TQV")


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data, f, allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":")
            )
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def parameter_name(gid):
    try:
        name = str(codes_get(gid, "shortName")).upper()
    except Exception:
        return None

    aliases = {
        "CAPE_ML": "CAPE_ML",
        "CIN_ML": "CIN_ML",
        "TQV": "TQV"
    }
    return aliases.get(name)


def decode(path):
    fields = {}
    with open(path, "rb") as stream:
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                name = parameter_name(gid)
                if name is None:
                    continue

                if name in fields:
                    raise ValueError(
                        "Parametro duplicato: " + name
                    )

                values = codes_get_array(gid, "values")
                lats = codes_get_array(gid, "latitudes")
                lons = codes_get_array(gid, "longitudes")

                if not (
                    len(values) == len(lats) == len(lons)
                ):
                    raise ValueError(
                        "Coordinate ICON non coerenti"
                    )

                fields[name] = (values, lats, lons)
            finally:
                codes_release(gid)

    missing = set(PARAMS) - set(fields)
    if missing:
        raise ValueError(
            "Parametri mancanti: " +
            ", ".join(sorted(missing))
        )
    return fields


def aggregate(fields):
    buckets = defaultdict(
        lambda: {p: [] for p in PARAMS}
    )
    west, east, south, north = REGION

    for name, (values, lats, lons) in fields.items():
        for value, lat, lon in zip(values, lats, lons):
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

            cell = (math.floor(lon), math.floor(lat))
            buckets[cell][name].append(value)

    return buckets


def classify(cape, cin, tqv):
    """Indice euristico, non calibrato."""
    if cape < 100 or cin < -250 or tqv < 10:
        return None
    if cape >= 2000 and cin >= -75 and tqv >= 30:
        return "A"
    if cape >= 1000 and cin >= -125 and tqv >= 22:
        return "B"
    if cape >= 250 and cin >= -200 and tqv >= 15:
        return "C"
    return None


def land_mask():
    import io
    import urllib.request
    import zipfile

    url = (
        "https://naturalearth.s3.amazonaws.com/"
        "50m_physical/ne_50m_land.zip"
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "SIBILLA/1.0"}
    )
    with urllib.request.urlopen(
        request, timeout=45
    ) as response:
        payload = response.read(12_000_001)

    if len(payload) > 12_000_000:
        raise ValueError("Maschera troppo grande")

    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        with zipfile.ZipFile(
            io.BytesIO(payload)
        ) as archive:
            for ext in ("shp", "shx", "dbf"):
                filename = f"ne_50m_land.{ext}"
                (folder / filename).write_bytes(
                    archive.read(filename)
                )

        reader = shapefile.Reader(
            str(folder / "ne_50m_land.shp")
        )
        extent = box(*REGION)
        geometries = []

        for item in reader.shapes():
            geom = shape(item.__geo_interface__)
            if geom.intersects(extent):
                geometries.append(
                    geom.intersection(extent)
                )
        reader.close()

    if not geometries:
        raise ValueError("Maschera terrestre vuota")
    return unary_union(geometries)


def polygon_parts(geom):
    if geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in (
        "MultiPolygon", "GeometryCollection"
    ):
        for item in geom.geoms:
            yield from polygon_parts(item)


def make_product(fields, horizon, valid_at, land):
    buckets = aggregate(fields)
    groups = defaultdict(list)
    statistics = defaultdict(list)

    for (x, y), data in buckets.items():
        if not all(data[p] for p in PARAMS):
            continue

        cape = max(data["CAPE_ML"])
        cin = max(data["CIN_ML"])
        tqv = sum(data["TQV"]) / len(data["TQV"])

        category = classify(cape, cin, tqv)
        if category is None:
            continue

        square = box(x, y, x + 1, y + 1)
        if not square.intersects(land):
            continue

        groups[category].append(square)
        statistics[category].append((cape, cin, tqv))

    features = []

    for category in ("A", "B", "C"):
        if not groups[category]:
            continue

        merged = unary_union(
            groups[category]
        ).intersection(land)

        if not merged.is_valid:
            merged = merged.buffer(0)

        values = statistics[category]
        count = len(values)

        for part in polygon_parts(merged):
            if part.is_empty or part.area <= 0:
                continue

            center = part.representative_point()
            features.append({
                "type": "Feature",
                "geometry": mapping(part),
                "properties": {
                    "class": category,
                    "label_lon": round(center.x, 5),
                    "label_lat": round(center.y, 5),
                    "cape_mean_j_kg": round(
                        sum(v[0] for v in values) / count
                    ),
                    "cin_mean_j_kg": round(
                        sum(v[1] for v in values) / count
                    ),
                    "pwat_mean_kg_m2": round(
                        sum(v[2] for v in values) / count, 1
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
            "model_id": "icon",
            "model": "DWD ICON-EU",
            "status": "ready",
            "horizon_hours": horizon,
            "forecast_valid_at": iso(valid_at),
            "generated_at": iso(utcnow()),
            "source": "DWD Open Data ICON-EU",
            "source_url": (
                "https://opendata.dwd.de/"
                "weather/nwp/v1/m/icon-eu/"
            ),
            "source_license": "CC BY 4.0",
            "parameters": list(PARAMS),
            "land_source": "Natural Earth 1:50m",
            "land_mask": True,
            "geometry_type":
                "merged_land_clipped_polygons",
            "geometry_resolution_degrees": 1,
            "probability_calibrated": False,
            "forecast_available": True,
            "index_type": "experimental_icon",
            "note": (
                "Indice sperimentale CAPE_ML/"
                "CIN_ML/TQV, non calibrato. "
                "Griglia nativa aggregata a 1 grado."
            )
        }
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    land = land_mask()

    with DWD() as dwd:
        model = dwd.nwp.model("icon-eu")
        available = {
            str(p).upper()
            for p in model.parameters()
        }
        missing = set(PARAMS) - available

        if missing:
            raise RuntimeError(
                "Parametri DWD non disponibili: " +
                ", ".join(sorted(missing))
            )

        for horizon in HOURS:
            errors = []

            # I passi sono relativi al ciclo del
            # modello, non all'ora attuale.
            # Si registra sempre l'ora effettiva.
            for step in range(
                horizon, horizon + 13
            ):
                try:
                    query = model.select(
                        list(PARAMS),
                        steps=f"{step}h"
                    )
                    plan = query.resolve()

                    with tempfile.TemporaryDirectory() as d:
                        target = Path(d) / "icon.grib2"
                        plan.download(str(target))
                        fields = decode(target)

                    # Non si presume che il ciclo
                    # sia uguale all'ora corrente.
                    run = plan.run
                    if hasattr(run, "datetime"):
                        cycle = run.datetime
                    else:
                        cycle = run

                    if isinstance(cycle, str):
                        cycle = datetime.fromisoformat(
                            cycle.replace("Z", "+00:00")
                        )
                    if cycle.tzinfo is None:
                        cycle = cycle.replace(
                            tzinfo=timezone.utc
                        )

                    from datetime import timedelta
                    valid_at = cycle + timedelta(hours=step)
                    delta = (
                        valid_at - utcnow()
                    ).total_seconds() / 3600

                    if not (horizon <= delta < horizon + 1):
                        continue

                    product = make_product(
                        fields, horizon, valid_at, land
                    )
                    product["metadata"][
                        "model_cycle"
                    ] = iso(cycle)

                    save(
                        OUT / f"{horizon}h.geojson",
                        product
                    )
                    results[str(horizon)] = {
                        "status": "ready",
                        "features": len(
                            product["features"]
                        ),
                        "valid_at": iso(valid_at)
                    }
                    print(f"ICON +{horizon}h: OK")
                    break

                except Exception as exc:
                    errors.append(
                        f"{step}h: "
                        f"{type(exc).__name__}: {exc}"
                    )
            else:
                results[str(horizon)] = {
                    "status": "unavailable",
                    "errors": errors[-5:]
                }
                stale = OUT / f"{horizon}h.geojson"
                if stale.exists():
                    stale.unlink()

    save(STATUS, {
        "generated_at": iso(utcnow()),
        "engine": "icon_eu_v1",
        "horizons": results
    })

    if not any(
        item["status"] == "ready"
        for item in results.values()
    ):
        raise RuntimeError(
            "Nessun prodotto ICON disponibile"
        )


if __name__ == "__main__":
    main()
      
