
#!/usr/bin/env python3
# S.I.B.I.L.L.A. V5
# Nowcasting Italia
# Acquisizione osservazioni I.R.I.D.E.
# Nessuna previsione sintetica.
# Nessuna modifica ai prodotti dei modelli.

import json
import os
import math
import tempfile
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OBS = DATA / "observations"

IRIDE_URL = (
    "https://19francomenenti73-dev.github.io/"
    "Iride/data/iride.json"
)

HORIZONS = (4, 8, 16, 24, 36)
RETENTION_HOURS = 80
MAX_AGE_MINUTES = 90


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Timestamp mancante")

    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        raise ValueError("Timestamp senza fuso orario")

    return dt.astimezone(timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path, obj):
    path.parent.mkdir(
        parents=True, exist_ok=True
    )

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp"
    )

    try:
        with os.fdopen(
            fd, "w", encoding="utf-8"
        ) as stream:
            json.dump(
                obj,
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False
            )

        os.replace(tmp, path)

    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def download_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SIBILLA-Research/5.0",
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(
        request, timeout=35
    ) as response:

        if response.status != 200:
            raise RuntimeError(
                f"HTTP {response.status}"
            )

        size = int(
            response.headers.get(
                "Content-Length", "0"
            )
        )

        if size > 30_000_000:
            raise ValueError(
                "File IRIDE troppo grande"
            )

        raw = response.read(30_000_001)

    if len(raw) > 30_000_000:
        raise ValueError(
            "Limite dimensione superato"
        )

    data = json.loads(raw)

    if not isinstance(data, dict):
        raise ValueError(
            "Documento IRIDE non valido"
        )

    return data


def valid_point(lat, lon):
    return (
        isinstance(lat, (int, float))
        and not isinstance(lat, bool)
        and isinstance(lon, (int, float))
        and not isinstance(lon, bool)
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def compact_cell(cell):
    lat = cell.get("lat")
    lon = cell.get("lon")

    if not valid_point(lat, lon):
        return None

    dbz = cell.get("max_dbz")

    if (
        not isinstance(dbz, (int, float))
        or isinstance(dbz, bool)
        or not math.isfinite(dbz)
    ):
        return None

    result = {
        "id": str(
            cell.get("id", "")
        )[:100],
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "max_dbz": round(dbz, 1)
    }

    area = cell.get("approx_area_km2")

    if (
        isinstance(area, (int, float))
        and not isinstance(area, bool)
        and math.isfinite(area)
        and area >= 0
    ):
        result["approx_area_km2"] = round(
            area, 2
        )

    return result


def acquire():
    raw = download_json(IRIDE_URL)

    if raw.get("status") != "ok":
        raise RuntimeError(
            "IRIDE non operativo"
        )

    observed_at = parse_time(
        raw.get("timestamp")
    )

    age = (
        utcnow() - observed_at
    ).total_seconds() / 60

    if age < -10 or age > MAX_AGE_MINUTES:
        raise RuntimeError(
            f"Radar non aggiornato: {age:.1f} min"
        )

    raw_cells = raw.get("cells", [])

    if not isinstance(raw_cells, list):
        raise ValueError(
            "Elenco celle IRIDE non valido"
        )

    cells = []

    for cell in raw_cells:
        if not isinstance(cell, dict):
            continue

        compact = compact_cell(cell)

        if compact is not None:
            cells.append(compact)

    source = raw.get("source") or {}

    if not isinstance(source, dict):
        source = {}

    snapshot = {
        "schema_version": 1,
        "product": "SIBILLA_RADAR_OBSERVATION",
        "observed_at": stamp(observed_at),
        "acquired_at": stamp(utcnow()),
        "source": {
            "provider": source.get("provider"),
            "product": source.get("product"),
            "license": source.get("license"),
            "attribution": source.get("attribution")
        },
        "cell_count": len(cells),
        "cells": cells,
        "geometry_quality": "centroids_only",
        "verification_ready": False
    }

    filename = observed_at.strftime(
        "%Y%m%dT%H%M%SZ.json"
    )

    atomic_json(
        OBS / filename,
        snapshot
    )

    atomic_json(
        DATA / "latest_observation.json",
        snapshot
    )

    return snapshot


def cleanup():
    OBS.mkdir(
        parents=True, exist_ok=True
    )

    cutoff = utcnow() - timedelta(
        hours=RETENTION_HOURS
    )

    removed = 0

    for path in OBS.glob("*.json"):
        try:
            observed = datetime.strptime(
                path.stem,
                "%Y%m%dT%H%M%SZ"
            ).replace(
                tzinfo=timezone.utc
            )

        except ValueError:
            continue

        if observed < cutoff:
            path.unlink()
            removed += 1

    return removed


def main():
    DATA.mkdir(
        parents=True, exist_ok=True
    )

    status = {
        "product": "SIBILLA",
        "version": 5,
        "generated_at": stamp(utcnow()),
        "horizons_hours": list(HORIZONS),
        "observation_engine": "IRIDE",
        "forecast_engine": "external_multimodel",
        "verification_engine": "not_connected",
        "radar_status": "unavailable"
    }

    try:
        snapshot = acquire()

        status["radar_status"] = "ok"
        status["radar_timestamp"] = (
            snapshot["observed_at"]
        )
        status["radar_cells"] = (
            snapshot["cell_count"]
        )

        print(
            "IRIDE acquisito:",
            snapshot["observed_at"],
            "celle:",
            snapshot["cell_count"]
        )

    except Exception as exc:
        status["radar_error"] = str(exc)

        print(
            "IRIDE non disponibile:",
            exc
        )

    status["expired_files_removed"] = cleanup()

    atomic_json(
        DATA / "status.json",
        status
    )

    print(
        "S.I.B.I.L.L.A. V5 - "
        "Acquisizione completata"
    )

    print(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2
        )
    )


if __name__ == "__main__":
    main()
    
