"""Conservative experimental radar-conditioned multimodel geometry.
No persistence assumption at forecast validity; short motion is a predictor only.
"""
from shapely.geometry import Point, shape, mapping
from shapely.ops import unary_union
from model_selector import load_models, parse_time

def assimilate(candidate, alternatives, corridor_points):
    # LAB B: land-clipped areas present in at least one alternative
    # model of the same class, supported by observed motion.
    if not alternatives or not corridor_points:
        return 0
    base = unary_union([shape(f["geometry"]) for f in candidate["features"]])
    additions = []
    for cls in ("A", "B", "C"):
        unions = [unary_union([shape(f["geometry"]) for f in p["features"]
                              if f["properties"]["class"] == cls]) for p in alternatives]
        unions = [g for g in unions if not g.is_empty and g.is_valid]
        if not unions:
            continue
        extra = unary_union(unions).difference(base)
        parts = [extra] if extra.geom_type == "Polygon" else list(getattr(extra, "geoms", []))
        for g in parts:
            if g.geom_type != "Polygon" or not g.is_valid or g.is_empty or g.area <= 0:
                continue
            tracks = sorted({str(key) for key, point in corridor_points if g.covers(point)})
            if not tracks:
                continue
            additions.append({"type":"Feature","geometry":mapping(g),"properties":{
                "class":cls,"experimental_observation_conditioned":True,
                "radar_track_ids":tracks,"radar_support_count":len(tracks),
                "alternative_models":[p["metadata"]["model_id"] for p in alternatives],
                "lab_variant":"B_single_alternative"}})
            base = base.union(g)
    candidate["features"].extend(additions)
    return len(additions)

def apply(candidate, horizon, corridor_points):
    if horizon != 4:
        return 0
    meta = candidate["metadata"]
    models, _ = load_models(horizon)
    valid = parse_time(meta["forecast_valid_at"])
    alternatives = [p for name,p in models.items() if name != meta["selected_model"]
                    and abs((parse_time(p["metadata"]["forecast_valid_at"])-valid).total_seconds()) <= 1800]
    return assimilate(candidate, alternatives, corridor_points)
