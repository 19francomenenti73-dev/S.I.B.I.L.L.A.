"""Immutable forecast archive and provisional radar-centroid verification."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from shapely.geometry import shape, Point
from shapely.ops import unary_union, nearest_points
from source_alignment import distance_km
from sibilla import atomic_json, parse_time

DATA = Path(__file__).resolve().parents[1] / "data"
ARCHIVE = DATA / "verification" / "forecasts"
METRICS = DATA / "verification" / "metrics"
HORIZONS = (4, 8, 16, 24, 36)

def main():
    now = datetime.now(timezone.utc)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    METRICS.mkdir(parents=True, exist_ok=True)
    for h in HORIZONS:
        p = DATA / "forecast" / f"{h}h.geojson"
        if not p.exists():
            continue
        raw = p.read_bytes()
        f = json.loads(raw)
        m = f["metadata"]
        if m["status"] != "ready" or parse_time(m["forecast_valid_at"]) <= now:
            continue
        key = hashlib.sha256(raw).hexdigest()
        target = ARCHIVE / f"{h}h_{key}.json"
        if not target.exists() and not (METRICS / target.name).exists():
            experimental = DATA / "experimental_forecast" / f"{h}h.geojson"
            candidate = json.loads(experimental.read_text()) if experimental.exists() else None
            if candidate and any(candidate["metadata"].get(k) != m.get(k) for k in ("generated_at","forecast_valid_at","selection_generated_at")):
                candidate = None
            atomic_json(target, {"archived_at": now.isoformat(), "forecast": f, "experimental": candidate})
    observations = {}
    for p in (DATA / "observations").glob("*.json"):
        o = json.loads(p.read_text())
        observations[parse_time(o["observed_at"])] = o
    completed = 0
    for p in ARCHIVE.glob("*.json"):
        a = json.loads(p.read_text())
        f = a["forecast"]
        m = f["metadata"]
        valid = parse_time(m["forecast_valid_at"])
        if now < valid + timedelta(minutes=45):
            continue
        rows = []
        for offset in (-30, 0, 30):
            target = valid + timedelta(minutes=offset)
            candidates = [t for t in observations if abs((t-target).total_seconds()) <= 900]
            if not candidates:
                rows.append({"offset_minutes": offset, "status": "missing_observation"})
                continue
            t = min(candidates, key=lambda x: abs(x-target))
            o = observations[t]
            cells = o["cells"]
            # Radar centroids are proxies, not confirmed convective events.
            geoms = [shape(x["geometry"]) for x in f["features"]]
            union = unary_union(geoms)
            points = [Point(c["lon"], c["lat"]) for c in cells]
            candidate = a.get("experimental")
            supported = unary_union([shape(x["geometry"]) for x in candidate["features"] if x["properties"].get("radar_support_count",0)>0]) if candidate else None
            distances = []
            if not union.is_empty:
                for point in points:
                    nearest = nearest_points(point, union)[1]
                    distances.append(distance_km({"lat":point.y,"lon":point.x},{"lat":nearest.y,"lon":nearest.x}))
            rows.append({"offset_minutes": offset, "status": "centroid_diagnostic",
                         "mean_distance_to_forecast_km": sum(distances)/len(distances) if distances else None,
                         "max_distance_to_forecast_km": max(distances) if distances else None,
                         "distance_sample_count": len(distances),
                         "centroids_in_radar_supported_polygons": sum(supported.covers(q) for q in points) if supported is not None and candidate["metadata"].get("radar_support_available") else None,
                         "observed_at": t.isoformat(), "sampling_offset_minutes": (t-target).total_seconds()/60,
                         "source": o.get("source"), "radar_centroids": len(points),
                         "centroids_inside": sum(union.covers(q) for q in points),
                         "centroids_outside": sum(not union.covers(q) for q in points),
                         "by_class": {k: sum(any(g.covers(q) for g,x in zip(geoms,f["features"]) if x["properties"]["class"] == k) for q in points) for k in ("A","B","C")}})
        result = {"forecast_id": p.stem, "horizon_hours": m["horizon_hours"],
                  "generated_at": m["generated_at"], "valid_at": valid.isoformat(),
                  "selected_model": m.get("selected_model"), "samples": rows,
                  "status": "provisional_centroid_diagnostic",
                  "note": "Coverage and convection not established; no false alarm or skill claims."}
        atomic_json(METRICS / p.name, result)
        completed += 1
        if now >= valid + timedelta(hours=72):
            p.unlink()
    atomic_json(DATA / "verification_status.json", {"generated_at": now.isoformat(), "processed": completed,
                "pending_archive_count": len(list(ARCHIVE.glob("*.json"))), "method": "centroid_diagnostic"})

if __name__ == "__main__":
    main()
