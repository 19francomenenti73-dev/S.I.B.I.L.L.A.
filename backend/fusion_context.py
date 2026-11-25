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
from shapely.geometry import Point, shape
from copy import deepcopy

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
              "sources": {"argo": aligned.get("argo", {}), "iride": aligned.get("iride", {}),
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
                    ids.append(v.get("id"))
            if ids:
                matches.append({"feature_index": index, "observed_track_ids": ids})
        valid = dt(metadata["forecast_valid_at"])
        compatible = [f for f in frames if isinstance(f, dict) and
                      isinstance(f.get("valid_utc"), str) and
                      abs((dt(f["valid_utc"]) - valid).total_seconds()) <= 5400]
        # Short-lived extrapolation only: never assume persistence for 4-36h.
        evidence = []
        if h in (4,8):
            for v in vectors:
                age_hours = (valid-dt(v["observed_at"])).total_seconds()/3600
                if not 0 <= age_hours <= 3:
                    continue
                lat,lon = map(math.radians,(v["lat"],v["lon"]))
                bearing = math.radians(v["direction_deg"])
                arc = v["speed_kmh"]*age_hours/6371
                q = math.asin(math.sin(lat)*math.cos(arc)+math.cos(lat)*math.sin(arc)*math.cos(bearing))
                x = lon+math.atan2(math.sin(bearing)*math.sin(arc)*math.cos(lat),math.cos(arc)-math.sin(lat)*math.sin(q))
                point = Point((math.degrees(x)+180)%360-180,math.degrees(q))
                hits = [i for i,f in enumerate(features) if shape(f["geometry"]).covers(point)]
                evidence.append({"track_id":v["id"],"source":v["source"],"projected_lon":point.x,"projected_lat":point.y,"feature_indices":hits})
        # Experimental ranking of existing model polygons by radar support.
        # No class changes or new risk areas; preserve baseline separately.
        candidate = deepcopy(forecast)
        for i, feature in enumerate(candidate["features"]):
            support = [e for e in evidence if i in e["feature_indices"]]
            feature["properties"]["radar_support_count"] = len(support)
            feature["properties"]["radar_support_sources"] = sorted({e["source"] for e in support})
        candidate["metadata"]["radar_assimilation"] = "experimental_polygon_support"
        candidate["metadata"]["radar_observed_at"] = aligned["observed_at"]
        candidate["metadata"]["radar_support_available"] = bool(evidence)
        candidate["metadata"]["radar_geometry_changed"] = False
        out = DATA / "experimental_forecast"
        out.mkdir(parents=True, exist_ok=True)
        atomic(out / f"{h}h.geojson", candidate)
        report["horizons"][str(h)] = {
            "projected_track_evidence": evidence,
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
