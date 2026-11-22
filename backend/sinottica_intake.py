#!/usr/bin/env python3
"""SIBILLA read-only diagnostic intake from NI SINOTTICA; no forecast modification."""
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

URL = "https://19francomenenti73-dev.github.io/Dashboard-Enterprise/data/sinottica.json"
OUT = Path(__file__).resolve().parents[1] / "data" / "sinottica_intake.json"

def timestamp(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Missing timezone")
    return dt.astimezone(timezone.utc)


SAT_URL = "https://19francomenenti73-dev.github.io/Dashboard-Enterprise/data/satellite_status.json"

def vector_grid(value, level, height):
    """Keep sampled model U/V values, without interpolation."""
    import math
    if not isinstance(value, dict) or value.get("units") != "m/s" or value.get(level) != height:
        return None
    lat, lon, u, v = (value.get(k) for k in ("lat", "lon", "u", "v"))
    if not all(isinstance(a, list) and a for a in (lat, lon, u, v)):
        return None
    if len(u) != len(lat) or len(v) != len(lat):
        return None
    if any(not isinstance(row, list) or len(row) != len(lon) for row in u + v):
        return None
    values = lat + lon + [x for row in u + v for x in row]
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
        return None
    return {"lat": lat, "lon": lon, "u": u, "v": v, "units": "m/s", level: height}

def satellite_metadata(now):
    """EUMETSAT WMS availability, NOT downloaded satellite imagery."""
    try:
        req = urllib.request.Request(SAT_URL, headers={"User-Agent": "SIBILLA-SINOTTICA/1.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = response.read(200_001)
        if len(payload) > 200_000:
            raise ValueError("Satellite status too large")
        data = json.loads(payload)
        checked = timestamp(data["checked_utc"])
        if checked > now + timedelta(minutes=10) or now - checked > timedelta(hours=3):
            raise ValueError("Satellite status expired")
        return {"status": "metadata_only", "checked_utc": checked.isoformat(),
                "source": data.get("source"), "products": data.get("products", {}),
                "imagery_ingested": False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "unavailable", "reason": str(exc)[:160],
                "imagery_ingested": False}

def main():
    request = urllib.request.Request(URL, headers={"User-Agent": "SIBILLA-SINOTTICA/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=35) as response:
        raw = response.read(25_000_001)
    if len(raw) > 25_000_000:
        raise ValueError("Input too large")
    data = json.loads(raw)
    now = datetime.now(timezone.utc)
    generated = timestamp(data["generated_utc"])
    if generated > now + timedelta(minutes=10) or now - generated > timedelta(hours=12):
        raise ValueError("Stale SINOTTICA source")
    if data.get("source_url") != "https://nomads.ncep.noaa.gov/":
        raise ValueError("Unexpected provenance")
    frames = []
    for frame in data.get("frames", []):
        try:
            if frame.get("source") != "NOAA/NCEP GFS 0.25° (model forecast)":
                continue
            run, valid = timestamp(frame["run_utc"]), timestamp(frame["valid_utc"])
            h = frame["forecast_hour"]
            if type(h) is not int or h < 0 or h > 120:
                continue
            if abs((valid - run).total_seconds() - 3600*h) > 60 or valid < now - timedelta(hours=4):
                continue
            frames.append({
                "run_utc": run.isoformat(), "valid_utc": valid.isoformat(), "forecast_hour": h,
                "fronts_validated": frame.get("fronts_status") == "validated",
                "front_candidates_present": isinstance(frame.get("frontal_candidates", {}).get("features"), list),
                "jet_250hpa_present": isinstance(frame.get("wind_vectors"), dict),
                "wind_10m_present": isinstance(frame.get("surface_wind_vectors"), dict),
                "pressure_present": isinstance(frame.get("pressure", {}).get("features"), list),
                "isobars_present": isinstance(frame.get("isobars", {}).get("features"), list),
                "jet_250hpa": vector_grid(frame.get("wind_vectors"), "level_hpa", 250),
                "wind_10m": vector_grid(frame.get("surface_wind_vectors"), "height_m", 10)
            })
        except (KeyError, TypeError, ValueError):
            continue
    if not frames:
        raise ValueError("No valid SINOTTICA frames")
    output = {
        "schema_version": 1, "product": "SIBILLA_SINOTTICA_DIAGNOSTIC",
        "mode": "read_only", "forecast_engine_connected": False,
        "source": "NOAA/NCEP GFS 0.25°", "source_url": URL,
        "acquired_utc": now.isoformat(), "model_generated_utc": generated.isoformat(),
        "frames": frames, "frame_count": len(frames),
        "satellite": satellite_metadata(now),
        "radar": {"source": "IRIDE", "acquisition": "existing sibilla.py",
                  "new_radar_download": False},
        "note": "Model forecasts, not observations. Front candidates are not validated fronts."
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=OUT.parent, prefix=".sinottica_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(output, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, OUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("SINOTTICA diagnostic frames:", len(frames))

if __name__ == "__main__":
    main()
