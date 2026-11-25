"""Immutable forecast archive and provisional radar-centroid verification."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from shapely.geometry import shape, Point, Polygon
from shapely.ops import transform
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
            context = None
            if candidate:
                try:
                    fusion = json.loads((DATA / "fusion_context.json").read_text())
                    aligned = json.loads((DATA / "aligned_inputs.json").read_text())
                    if fusion.get("observed_at") == candidate["metadata"].get("radar_observed_at") == aligned.get("observed_at") and abs((now-parse_time(fusion["generated_at"])).total_seconds()) <= 600:
                        context = {"fusion": fusion["horizons"].get(str(h), {}), "sources": fusion.get("sources", {}), "observed_at": aligned["observed_at"], "archived_at": now.isoformat(), "forecast_modified": False}
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            atomic_json(target, {"archived_at": now.isoformat(), "forecast": f, "experimental": candidate, "source_context": context})
    observations = {}
    for p in (DATA / "observations").glob("*.json"):
        o = json.loads(p.read_text())
        observations[parse_time(o["observed_at"])] = o
    completed = 0
    reports = 0
    missing_samples = 0
    for p in ARCHIVE.glob("*.json"):
        a = json.loads(p.read_text())
        f = a["forecast"]
        m = f["metadata"]
        valid = parse_time(m["forecast_valid_at"])
        if now < valid + timedelta(minutes=45):
            continue
        previous = METRICS / p.name
        if previous.exists() and now >= valid + timedelta(hours=72):
            p.unlink()
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
            # Bounding envelopes are not actual echo footprints. Report proxy only.
            envelopes = []
            for c in cells:
                ring = c.get("perimeter")
                if isinstance(ring,list) and len(ring)>=3:
                    try:
                        g = Polygon([(p[1],p[0]) for p in ring])
                        if g.is_valid and not g.is_empty:
                            envelopes.append(g)
                    except (ValueError,TypeError,IndexError):
                        pass
            proxy = unary_union(envelopes)
            overlap = None
            if envelopes and not union.is_empty:
                # Approximate equal-area cylindrical projection, km units.
                import math
                def project(x,y,z=None):
                    return (6371*math.radians(x),6371*math.sin(math.radians(y)))
                obs_area = transform(project,proxy)
                pred_area = transform(project,union)
                intersection = obs_area.intersection(pred_area).area
                overlap = {"observed_envelope_km2":obs_area.area,"intersection_km2":intersection,
                           "observed_envelope_fraction_inside":intersection/obs_area.area if obs_area.area else None,
                           "envelope_count":len(envelopes),"method":"bounding_envelope_proxy_not_echo_coverage"}
            distances = []
            if not union.is_empty:
                for point in points:
                    nearest = nearest_points(point, union)[1]
                    distances.append(distance_km({"lat":point.y,"lon":point.x},{"lat":nearest.y,"lon":nearest.x}))
            rows.append({"offset_minutes": offset, "status": "centroid_diagnostic",
                         "mean_distance_to_forecast_km": sum(distances)/len(distances) if distances else None,
                         "max_distance_to_forecast_km": max(distances) if distances else None,
                         "distance_sample_count": len(distances),
                         "envelope_overlap_proxy": overlap,
                         "false_alarm_rate": None, "miss_rate": None,
                         "coverage_verified": False,
                         "centroids_in_radar_supported_polygons": sum(supported.covers(q) for q in points) if supported is not None and candidate["metadata"].get("radar_support_available") else None,
                         "observed_at": t.isoformat(), "sampling_offset_minutes": (t-target).total_seconds()/60,
                         "source": o.get("source"), "radar_centroids": len(points),
                         "centroids_inside": sum(union.covers(q) for q in points),
                         "centroids_outside": sum(not union.covers(q) for q in points),
                         "by_class": {k: sum(any(g.covers(q) for g,x in zip(geoms,f["features"]) if x["properties"]["class"] == k) for q in points) for k in ("A","B","C")}})
        result = {"forecast_id": p.stem, "horizon_hours": m["horizon_hours"],
                  "generated_at": m["generated_at"], "valid_at": valid.isoformat(),
                  "selected_model": m.get("selected_model"), "source_context": a.get("source_context"), "samples": rows,
                  "status": "provisional_centroid_diagnostic",
                  "note": "Coverage and convection not established; no false alarm or skill claims."}
        atomic_json(METRICS / p.name, result)
        reports += 1
        missing_samples += sum(row["status"] == "missing_observation" for row in rows)
        completed += int(any(row["status"] == "centroid_diagnostic" for row in rows))
        if now >= valid + timedelta(hours=72):
            p.unlink()
    atomic_json(DATA / "verification_status.json", {"generated_at": now.isoformat(), "processed": completed, "reports_written": reports, "missing_samples": missing_samples,
                "pending_archive_count": len(list(ARCHIVE.glob("*.json"))), "method": "centroid_diagnostic"})

if __name__ == "__main__":
    main()
