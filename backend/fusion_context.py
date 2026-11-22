#!/usr/bin/env python3
"""Attach evidence-based observational context to SIBILLA forecasts.
Does not alter model geometries, classes, horizons, or forecast validity.
Current radar observations are NOT verification of future forecasts.
"""
import json
import math
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
HORIZONS = (4, 8, 16, 24, 36)

def dt(value):
    x = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if x.tzinfo is None:
        raise ValueError("Timestamp missing timezone")
    return x.astimezone(timezone.utc)

def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def atomic(path, obj):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".fusion_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def inside_ring(lon, lat, ring):
    hit = False
    if len(ring) < 4:
        return False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        x1, y1 = a[:2]
        x2, y2 = b[:2]
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing:
                hit = not hit
    return hit

def inside_geometry(lon, lat, geometry):
    typ = geometry.get("type")
    polys = [geometry.get("coordinates", [])] if typ == "Polygon" else geometry.get("coordinates", []) if typ == "MultiPolygon" else []
    for polygon in polys:
        if polygon and inside_ring(lon, lat, polygon[0]):
            if not any(inside_ring(lon, lat, hole) for hole in polygon[1:]):
                return True
    return False

def main():
    now = datetime.now(timezone.utc)
    aligned = read(DATA / "aligned_inputs.json")
    if not isinstance(aligned, dict) or aligned.get("product") != "SIBILLA_ALIGNED_INPUTS":
        raise RuntimeError("Aligned inputs unavailable")
    age = now - dt(aligned["observed_at"])
    if not -timedelta(minutes=10) <= age <= timedelta(minutes=90):
        raise RuntimeError("Aligned observations stale")
    vectors = aligned.get("vectors", [])
    if not isinstance(vectors, list):
        raise RuntimeError("Invalid aligned vectors")
    sinottica = read(DATA / "sinottica_intake.json")
    frames = sinottica.get("frames", []) if isinstance(sinottica, dict) else []
    report = {"generated_at": now.isoformat(), "product": "SIBILLA_SOURCE_FUSION",
              "observed_at": aligned["observed_at"],
              "sources": {"argo": len(vectors), "iride": aligned.get("iride", {}),
                          "sinottica_frames": len(frames)},
              "horizons": {}, "forecast_modified": False}
    for h in HORIZONS:
        path = DATA / "forecast" / f"{h}h.geojson"
        forecast = read(path)
        if not isinstance(forecast, dict) or forecast.get("type") != "FeatureCollection":
            report["horizons"][str(h)] = {"status": "unavailable"}
            continue
        metadata = forecast.get("metadata", {})
        if metadata.get("horizon_hours") != h or metadata.get("status") != "ready":
            report["horizons"][str(h)] = {"status": "invalid"}
            continue
        features = forecast.get("features", [])
        matches = []
        for index, feature in enumerate(features):
            geo = feature.get("geometry", {})
            ids = []
            for v in vectors:
                lat, lon = v.get("lat"), v.get("lon")
                if type(lat) not in (float, int) or type(lon) not in (float, int):
                    continue
                if not math.isfinite(lat) or not math.isfinite(lon):
                    continue
                if inside_geometry(lon, lat, geo):
                    ids.append(v.get("argo_id"))
            if ids:
                matches.append({"feature_index": index, "observed_argo_ids": ids})
        valid = dt(metadata["forecast_valid_at"])
        compatible = [f for f in frames if isinstance(f, dict) and
                      isinstance(f.get("valid_utc"), str) and
                      abs((dt(f["valid_utc"]) - valid).total_seconds()) <= 5400]
        report["horizons"][str(h)] = {
            "status": "diagnostic_only", "selected_model": metadata.get("selected_model"),
            "polygon_matches_at_observation_time": matches,
            "sinottica_time_aligned_frames": len(compatible),
            "radar_is_future_verification": False,
            "forecast_classes_changed": False
        }
    atomic(DATA / "fusion_context.json", report)
    print("SIBILLA observational context written; forecast products untouched")

if __name__ == "__main__":
    main()
