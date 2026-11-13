
#!/usr/bin/env python3
"""S.I.B.I.L.L.A. - GFS experimental convective index V2.
NOAA NOMADS GRIB2. No calibrated probabilities or alerts.
"""

import json
import math
import os
import tempfile
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eccodes import (
    codes_get,
    codes_get_array,
    codes_grib_new_from_file,
    codes_release,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "forecast"
HOURS = (3, 24, 48, 72)
BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
REGION = (-5.0, 38.0, 30.0, 48.0)
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path, data):
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
    start = NOW.replace(
        hour=(NOW.hour // 6) * 6,
        minute=0, second=0, microsecond=0
    )
    for back in (1, 2, 3):
        yield start - timedelta(hours=back * 6)


def forecast_hour(cycle, horizon):
    elapsed = (NOW - cycle).total_seconds() / 3600
    lead = int(math.ceil((elapsed + horizon) / 3) * 3)
    return max(3, min(120, lead))


def url_for(cycle, lead):
    day = cycle.strftime("%Y%m%d")
    hour = cycle.strftime("%H")
    params = {
        "file": f"gfs.t{hour}z.pgrb2.0p25.f{lead:03d}",
        "dir": f"/gfs.{day}/{hour}/atmos",
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
            "User-Agent": "SIBILLA-Research/2.0",
            "Accept": "application/octet-stream",
        }
    )
    with urllib.request.urlopen(request, timeout=75) as response:
        payload = response.read(30_000_001)

    if len(payload) > 30_000_000:
        raise ValueError("GRIB2 oltre limite dimensionale")
    if not payload.startswith(b"GRIB"):
        raise ValueError("NOAA non ha restituito GRIB2")
    return payload


def safe_get(gid, key):
    try:
        return str(codes_get(gid, key))
    except Exception:
        return ""


def identify(gid):
    short = safe_get(gid, "shortName").lower()
    name = safe_get(gid, "name").lower()

    if short == "cape" or "convective available potential" in name:
        return "cape"
    if short in ("cin", "cins") or "convective inhibition" in name:
        return "cin"
    if short == "pwat" or "precipitable water" in name:
        return "pwat"
    return None


def priority(name, level):
    level = level.lower()
    if name == "pwat":
        if "entireatmosphere" in level:
            return 4
        return 1
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
                short = safe_get(gid, "shortName")
                inventory.append({
                    "short": short,
                    "level": level,
                    "name": safe_get(gid, "name"),
                })
                if name is None:
                    continue

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

    print("GRIB inventory:", json.dumps(
        inventory, ensure_ascii=False
    ))

    missing = sorted(
        {"cape", "cin", "pwat"} - set(fields)
    )
    if missing:
        raise ValueError(
            "Campi mancanti: " + ",".join(missing)
            + " | disponibili: "
            + ",".join(sorted(set(fields)))
        )

    return fields


def aggregate(fields):
    buckets = defaultdict(
        lambda: {"cape": [], "cin": [], "pwat": []}
    )
    west, east, south, north = REGION

    for name, (_, values, lats, lons) in fields.items():
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

            key = (math.floor(lon), math.floor(lat))
            buckets[key][name].append(value)

    return buckets


def grade(cape, cin, pwat):
    # Soglie sperimentali, NON probabilita' calibrate.
    # CIN viene qui trattato come valore negativo.
    if cape < 100 or cin < -250 or pwat < 10:
        return "D"
    if cape >= 2000 and cin >= -75 and pwat >= 30:
        return "A"
    if cape >= 1000 and cin >= -125 and pwat >= 22:
        return "B"
    if cape >= 250 and cin >= -200 and pwat >= 15:
        return "C"
    return "D"


def geojson(fields, cycle, lead, horizon):
    features = []
    buckets = aggregate(fields)

    for (x, y), item in sorted(buckets.items()):
        if not all(
            item[k] for k in ("cape", "cin", "pwat")
        ):
            continue

        cape = max(item["cape"])
        cin = max(item["cin"])
        pwat = sum(item["pwat"]) / len(item["pwat"])
        category = grade(cape, cin, pwat)

        if category == "D":
            continue

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [x, y], [x + 1, y],
                    [x + 1, y + 1], [x, y + 1],
                    [x, y],
                ]],
            },
            "properties": {
                "class": category,
                "category": category,
                "cape_j_kg": round(cape),
                "cin_j_kg": round(cin),
                "pwat_kg_m2": round(pwat, 1),
                "probability_calibrated": False,
                "observed": False,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "product": "SIBILLA",
            "status": "ready",
            "model": "NOAA GFS 0.25 degree",
            "index_type": "experimental_convective_potential",
            "forecast_available": True,
            "probability_calibrated": False,
            "horizon_hours": horizon,
            "model_cycle": iso(cycle),
            "forecast_valid_at": iso(
                cycle + timedelta(hours=lead)
            ),
            "generated_at": iso(NOW),
            "source": "NOAA NOMADS GFS GRIB2",
            "geometry_resolution_degrees": 1,
            "note": "Indice euristico non calibrato",
        },
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}

    for horizon in HOURS:
        errors = []

        for cycle in cycles():
            lead = forecast_hour(cycle, horizon)

            try:
                payload = download(cycle, lead)
                fields = decode(payload)
                product = geojson(
                    fields, cycle, lead, horizon
                )

                atomic_json(
                    OUT / f"{horizon}h.geojson",
                    product
                )
                results[str(horizon)] = {
                    "status": "ready",
                    "features": len(product["features"]),
                    "valid_at": product[
                        "metadata"
                    ]["forecast_valid_at"],
                }
                print(
                    f"GFS +{horizon}h OK: "
                    f"{len(product['features'])} poligoni"
                )
                break

            except Exception as exc:
                message = (
                    f"{iso(cycle)} "
                    f"{type(exc).__name__}: {exc}"
                )
                errors.append(message)
                print(f"GFS +{horizon}h: {message}")

        else:
            results[str(horizon)] = {
                "status": "unavailable",
                "errors": errors,
            }

    atomic_json(DATA / "gfs_status.json", {
        "generated_at": iso(NOW),
        "engine": "experimental_gfs_v2",
        "horizons": results,
    })

    if not any(
        item["status"] == "ready"
        for item in results.values()
    ):
        raise RuntimeError(
            "Nessuna scadenza GFS disponibile"
        )


if __name__ == "__main__":
    main()
    
