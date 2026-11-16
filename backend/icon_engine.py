
#!/usr/bin/env python3
"""S.I.B.I.L.L.A. ICON-EU V4 - decoder diagnostico."""
import io
import json
import math
import os
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import shapefile
from dwdopen import DWD
from eccodes import (
    codes_grib_new_from_file,
    codes_grib_get_data,
    codes_get,
    codes_get_array,
    codes_release,
)
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/models/icon"
STATUS = ROOT / "data/icon_status.json"
HOURS = (4, 8, 16, 24, 36)
PARAMS = ("CAPE_ML", "CIN_ML", "TQV")
REGION = (-5.0, 38.0, 30.0, 48.0)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def optional_key(gid, key):
    try:
        return codes_get(gid, key)
    except Exception:
        return None


def parse_cycle(value, depth=0):
    if value is None or depth > 5:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, str):
        s = value.strip()
        try:
            if s.isdigit() and len(s) in (10, 12, 14):
                fmt = {
                    10: "%Y%m%d%H",
                    12: "%Y%m%d%H%M",
                    14: "%Y%m%d%H%M%S",
                }[len(s)]
                return datetime.strptime(
                    s, fmt
                ).replace(tzinfo=timezone.utc)
            return parse_cycle(
                datetime.fromisoformat(
                    s.replace("Z", "+00:00")
                ), depth + 1
            )
        except ValueError:
            return None

    if isinstance(value, dict):
        for key in (
            "datetime", "reference_time", "run_time",
            "init_time", "initial_time", "cycle",
            "date_time", "timestamp", "date",
        ):
            if key in value:
                result = parse_cycle(
                    value[key], depth + 1
                )
                if result:
                    return result
        return None

    for attr in (
        "datetime", "reference_time", "run_time",
        "init_time", "initial_time", "cycle",
        "date_time", "timestamp", "date",
    ):
        try:
            child = getattr(value, attr)
            if callable(child):
                child = child()
            result = parse_cycle(child, depth + 1)
            if result:
                return result
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def cycle_datetime(plan):
    run = getattr(plan, "run", None)
    result = parse_cycle(run)

    if result is None:
        print("DWD RUN TYPE:", type(run).__name__,
              flush=True)
        print("DWD RUN REPR:", repr(run)[:400],
              flush=True)
        raise ValueError("Ciclo DWD non interpretabile")

    if result > now() + timedelta(minutes=15):
        raise ValueError("Ciclo DWD nel futuro")
    if now() - result > timedelta(hours=24):
        raise ValueError("Ciclo DWD troppo vecchio")
    return result


def identify_parameter(gid, index):
    keys = (
        "shortName", "name", "paramId",
        "discipline", "parameterCategory",
        "parameterNumber", "typeOfLevel",
        "gridType", "numberOfPoints",
    )
    info = {k: optional_key(gid, k) for k in keys}

    print(
        "GRIB MESSAGE", index,
        json.dumps(info, default=str),
        flush=True
    )

    short = str(info["shortName"] or "").upper()
    name = str(info["name"] or "").upper()

    for param in PARAMS:
        if short == param:
            return param

    if "CONVECTIVE AVAILABLE POTENTIAL" in name:
        if "MIXED" in name:
            return "CAPE_ML"
    if "CONVECTIVE INHIBITION" in name:
        if "MIXED" in name:
            return "CIN_ML"
    if "TOTAL COLUMN" in name and "WATER" in name:
        return "TQV"

    return None


def read_grid(gid, param):
    # Tentativo 1: accesso diretto alle coordinate.
    try:
        values = codes_get_array(gid, "values")
        lats = codes_get_array(gid, "latitudes")
        lons = codes_get_array(gid, "longitudes")
        method = "coordinate_arrays"
    except Exception as exc:
        print(
            f"GRIB {param}: coordinate_arrays fallito: "
            f"{type(exc).__name__}: {exc}",
            flush=True
        )

        # Tentativo 2: iteratore geografico ecCodes.
        try:
            lats, lons, values = codes_grib_get_data(gid)
            method = "grib_get_data"
        except Exception as second:
            raise RuntimeError(
                f"Coordinate {param} non disponibili; "
                f"arrays={type(exc).__name__}: {exc}; "
                f"iterator={type(second).__name__}: "
                f"{second}"
            ) from second

    if not (
        len(values) == len(lats) == len(lons)
    ):
        raise ValueError(
            f"Dimensioni incoerenti per {param}: "
            f"values={len(values)}, "
            f"latitudes={len(lats)}, "
            f"longitudes={len(lons)}"
        )

    print(
        f"GRIB {param}: {len(values)} punti, "
        f"metodo={method}",
        flush=True
    )
    return values, lats, lons


def decode(path):
    fields = {}
    count = 0
    unknown = []

    with path.open("rb") as stream:
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break

            count += 1
            try:
                param = identify_parameter(gid, count)
                if param is None:
                    unknown.append(count)
                    continue
                if param in fields:
                    raise ValueError(
                        f"Parametro duplicato: {param}"
                    )
                fields[param] = read_grid(gid, param)
            finally:
                codes_release(gid)

    print(
        "GRIB TOTAL:", count,
        "UNKNOWN:", unknown,
        "RECOGNIZED:", sorted(fields),
        flush=True
    )

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
        for v, lat, lon in zip(values, lats, lons):
            v = float(v)
            lat = float(lat)
            lon = float(lon)

            if lon > 180:
                lon -= 360

            if not (
                west <= lon < east
                and south <= lat < north
                and math.isfinite(v)
                and abs(v) < 1e8
            ):
                continue

            key = (math.floor(lon), math.floor(lat))
            buckets[key][name].append(v)
    return buckets


def classify(cape, cin, tqv):
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
    url = (
        "https://naturalearth.s3.amazonaws.com/"
        "50m_physical/ne_50m_land.zip"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "SIBILLA/4.0"}
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError("Maschera terrestre troppo grande")

    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for ext in ("shp", "shx", "dbf"):
                name = f"ne_50m_land.{ext}"
                (folder / name).write_bytes(z.read(name))

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
        for part in geom.geoms:
            yield from polygon_parts(part)


def make_product(fields, horizon, valid, land):
    groups = defaultdict(list)
    stats = defaultdict(list)

    for (x, y), data in aggregate(fields).items():
        if not all(data[p] for p in PARAMS):
            continue

        cape = max(data["CAPE_ML"])
        cin = max(data["CIN_ML"])
        tqv = sum(data["TQV"]) / len(data["TQV"])

        cls = classify(cape, cin, tqv)
        if cls is None:
            continue

        cell = box(x, y, x + 1, y + 1)
        if not cell.intersects(land):
            continue

        groups[cls].append(cell)
        stats[cls].append((cape, cin, tqv))

    features = []
    for cls in ("A", "B", "C"):
        if not groups[cls]:
            continue

        geom = unary_union(groups[cls]).intersection(land)
        if not geom.is_valid:
            geom = geom.buffer(0)

        vals = stats[cls]
        n = len(vals)

        for part in polygon_parts(geom):
            if part.is_empty or part.area <= 0:
                continue
            center = part.representative_point()

            features.append({
                "type": "Feature",
                "geometry": mapping(part),
                "properties": {
                    "class": cls,
                    "label_lon": round(center.x, 5),
                    "label_lat": round(center.y, 5),
                    "cape_mean_j_kg": round(
                        sum(v[0] for v in vals) / n
                    ),
                    "cin_mean_j_kg": round(
                        sum(v[1] for v in vals) / n
                    ),
                    "pwat_mean_kg_m2": round(
                        sum(v[2] for v in vals) / n, 1
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
            "forecast_valid_at": iso(valid),
            "generated_at": iso(now()),
            "source": "DWD Open Data ICON-EU",
            "source_url": "https://opendata.dwd.de/",
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
            "note": "Indice sperimentale non calibrato."
        }
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    downloads = 0
    land = land_mask()

    with DWD() as dwd:
        model = dwd.nwp.model("icon-eu")
        available = {
            str(p).upper() for p in model.parameters()
        }
        missing = set(PARAMS) - available
        if missing:
            raise RuntimeError(
                "Parametri DWD non disponibili: " +
                ",".join(sorted(missing))
            )

        probe = model.select(
            list(PARAMS), steps="4h"
        ).resolve()
        cycle = cycle_datetime(probe)
        reference = now()

        print("ICON cycle:", iso(cycle), flush=True)

        for h in HOURS:
            path = OUT / f"{h}h.geojson"
            try:
                elapsed = (
                    reference - cycle
                ).total_seconds() / 3600
                step = max(1, math.ceil(elapsed + h))
                valid = cycle + timedelta(hours=step)

                plan = model.select(
                    list(PARAMS), steps=f"{step}h"
                ).resolve()

                actual = cycle_datetime(plan)
                if actual != cycle:
                    raise ValueError("Ciclo DWD cambiato")

                with tempfile.TemporaryDirectory() as d:
                    target = Path(d) / "icon.grib2"
                    downloads += 1
                    plan.download(str(target))
                    fields = decode(target)

                product = make_product(
                    fields, h, valid, land
                )
                product["metadata"]["model_cycle"] = (
                    iso(cycle)
                )
                product["metadata"]["model_step_hours"] = (
                    step
                )
                save(path, product)

                results[str(h)] = {
                    "status": "ready",
                    "features": len(product["features"]),
                    "step_hours": step,
                    "valid_at": iso(valid)
                }
                print(
                    f"ICON +{h}h: OK step={step}",
                    flush=True
                )

            except Exception as exc:
                path.unlink(missing_ok=True)
                results[str(h)] = {
                    "status": "unavailable",
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    )
                }
                print(
                    f"ICON +{h}h: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True
                )

    save(STATUS, {
        "generated_at": iso(now()),
        "engine": "icon_eu_v4",
        "model_cycle": iso(cycle),
        "downloads_attempted": downloads,
        "maximum_downloads": 5,
        "horizons": results
    })

    if not any(
        item["status"] == "ready"
        for item in results.values()
    ):
        raise RuntimeError(
            "Nessun prodotto ICON valido"
        )


if __name__ == "__main__":
    main()
    
