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

def sample_wind(grid, lon, lat):
    if not grid:
        return None
    ys, xs = grid["lat"], grid["lon"]
    if not min(ys) <= lat <= max(ys) or not min(xs) <= lon <= max(xs):
        return None
    i = min(range(len(ys)), key=lambda i: abs(ys[i]-lat))
    j = min(range(len(xs)), key=lambda j: abs(xs[j]-lon))
    u, v = grid["u"][i][j], grid["v"][i][j]
    return {"u_ms": u, "v_ms": v, "speed_kmh": round(math.hypot(u,v)*3.6,2),
            "grid_lat": ys[i], "grid_lon": xs[j]}

def sample_pressure(grid, lon, lat):
    if not grid:
        return None
    ys,xs,z = grid["lat"],grid["lon"],grid["values"]
    if not min(ys)<=lat<=max(ys) or not min(xs)<=lon<=max(xs):
        return None
    i=min(range(len(ys)),key=lambda k:abs(ys[k]-lat))
    j=min(range(len(xs)),key=lambda k:abs(xs[k]-lon))
    result={"mslp_hpa":z[i][j],"grid_lat":ys[i],"grid_lon":xs[j]}
    if 0<i<len(ys)-1 and 0<j<len(xs)-1:
        dx=111.195*math.cos(math.radians(ys[i]))*(xs[j+1]-xs[j-1])
        dy=111.195*(ys[i+1]-ys[i-1])
        if abs(dx)>1e-6 and abs(dy)>1e-6:
            east=100*(z[i][j+1]-z[i][j-1])/dx
            north=100*(z[i+1][j]-z[i-1][j])/dy
            result.update({"gradient_east_hpa_per_100km":round(east,3),"gradient_north_hpa_per_100km":round(north,3),"gradient_magnitude_hpa_per_100km":round(math.hypot(east,north),3)})
    return result

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
    frames = []
    if isinstance(sinottica, dict):
        try:
            if -timedelta(minutes=10) <= now-dt(sinottica["acquired_utc"]) <= timedelta(minutes=90) and -timedelta(minutes=10) <= now-dt(sinottica["model_generated_utc"]) <= timedelta(hours=12):
                frames = sinottica.get("frames", [])
        except (KeyError, ValueError, TypeError):
            pass
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
        # Current motion corridors are contextual evidence, not survival forecasts.
        corridor = []
        if h in (4,8):
            for v in vectors:
                if not 0 <= (now-dt(v["observed_at"])).total_seconds() <= 5400:
                    continue
                lat,lon = map(math.radians,(v["lat"],v["lon"]))
                b = math.radians(v["direction_deg"])
                for minutes in (0,30,60,90,120,180):
                    arc = v["speed_kmh"]*minutes/60/6371
                    q = math.asin(math.sin(lat)*math.cos(arc)+math.cos(lat)*math.sin(arc)*math.cos(b))
                    x = lon+math.atan2(math.sin(b)*math.sin(arc)*math.cos(lat),math.cos(arc)-math.sin(lat)*math.sin(q))
                    point = Point((math.degrees(x)+180)%360-180,math.degrees(q))
                    hits = [i for i,f in enumerate(features) if shape(f["geometry"]).covers(point)]
                    corridor.append({"track_id":v["id"],"source":v["source"],"minutes":minutes,"feature_indices":hits})
        frame = min(compatible, key=lambda f: abs((dt(f["valid_utc"])-valid).total_seconds())) if compatible else None
        context_count = 0
        pressure_count = 0
        for i, feature in enumerate(candidate["features"]):
            if frame:
                point = shape(feature["geometry"]).representative_point()
                jet = sample_wind(frame.get("jet_250hpa"), point.x, point.y)
                surface = sample_wind(frame.get("wind_10m"), point.x, point.y)
                pressure = sample_pressure(frame.get('pressure_grid'), point.x, point.y)
                if pressure:
                    pressure_count += 1
                if jet or surface or pressure:
                    context_count += 1
                    feature["properties"]["sinottica_context"] = {
                        "jet_250hpa": jet, "wind_10m": surface, "pressure": pressure,
                        "valid_at": frame["valid_utc"], "run_at": frame["run_utc"],
                        "source": "NOAA GFS", "independent_model": False,
                        "sampling": "nearest_grid_at_polygon_representative_point"
                    }
            support = [e for e in evidence if i in e["feature_indices"]]
            feature["properties"]["radar_corridor_track_count"] = len({(e["source"],e["track_id"]) for e in corridor if i in e["feature_indices"]})
            feature["properties"]["radar_support_count"] = len(support)
            feature["properties"]["radar_support_sources"] = sorted({e["source"] for e in support})
        candidate["metadata"]["sinottica_context_count"] = context_count
        candidate["metadata"]["sinottica_forecast_modified"] = False
        candidate["metadata"]["radar_assimilation"] = "experimental_polygon_support"
        candidate["metadata"]["radar_observed_at"] = aligned["observed_at"]
        candidate["metadata"]["radar_support_available"] = bool(evidence)
        candidate["metadata"]["radar_geometry_changed"] = False
        out = DATA / "experimental_forecast"
        out.mkdir(parents=True, exist_ok=True)
        atomic(out / f"{h}h.geojson", candidate)
        report["horizons"][str(h)] = {
            "projected_track_evidence": evidence,
            "corridor_context_samples": len(corridor),
            "status": "diagnostic_only", "selected_model": metadata.get("selected_model"),
            "polygon_matches_at_observation_time": matches,
            "sinottica_time_aligned_frames": len(compatible),
            "sinottica_context_polygons": context_count,
            "sinottica_pressure_polygons": pressure_count,
            "radar_is_future_verification": False,
            "forecast_classes_changed": False
        }
    atomic(DATA / "fusion_context.json", report)
    print("SIBILLA observational context written; forecast products untouched")

if __name__ == "__main__":
    main()
