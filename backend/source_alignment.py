#!/usr/bin/env python3
"""Align ARGO radar tracks with IRIDE observations and SINOTTICA model frames.
Observational and model data remain distinct; no fabricated forecast skill.
"""
import json
import math
import os
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARGO_URLS = (
    "https://raw.githubusercontent.com/19francomenenti73-dev/A.R.G.O./main/data/",
    "https://19francomenenti73-dev.github.io/A.R.G.O./data/",
)
MAX_AGE = timedelta(minutes=90)

def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.astimezone(timezone.utc)

def fetch(name):
    if name not in ("argo.json", "argo_history.json"):
        raise ValueError("Unexpected ARGO file")
    errors = []
    for base in ARGO_URLS:
        try:
            req = urllib.request.Request(
                base + name,
                headers={"User-Agent": "SIBILLA-ARGO/1.1", "Accept": "application/json",
                         "Cache-Control": "no-cache"}
            )
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("ARGO payload too large")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("ARGO JSON root must be object")
            print("ARGO source:", base, name)
            return data
        except (OSError, ValueError, TimeoutError) as exc:
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
    raise RuntimeError("ARGO acquisition failed: " + " | ".join(errors))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def coordinates(p):
    return isinstance(p, dict) and finite(p.get("lat")) and finite(p.get("lon")) and -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180

def distance_km(a, b):
    r = math.pi / 180
    x = math.sin((a["lat"] - b["lat"]) * r / 2) ** 2
    x += math.cos(a["lat"] * r) * math.cos(b["lat"] * r) * math.sin((a["lon"] - b["lon"]) * r / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, max(0, x))))

def load_local(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".align_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def bearing(a, b):
    r = math.pi / 180
    p, q = a["lat"]*r, b["lat"]*r
    d = (b["lon"]-a["lon"])*r
    return math.degrees(math.atan2(math.sin(d)*math.cos(q), math.cos(p)*math.sin(q)-math.sin(p)*math.cos(q)*math.cos(d))) % 360

def normalize(cell, obs, observed, source):
    if not coordinates(cell) or len(obs) < 2:
        return None
    a,b = obs[-2:]
    if not coordinates(a) or not coordinates(b):
        return None
    t0,t1 = timestamp(a["timestamp"]),timestamp(b["timestamp"])
    dt = (t1-t0).total_seconds()/60
    if not 4 <= dt <= (30 if source=="argo" else 45) or abs((t1-observed).total_seconds()) > 60:
        return None
    speed = distance_km(a,b)*60/dt
    if speed > (130 if source=="argo" else 140) or distance_km(b,cell)>2:
        return None
    return {"id":str(cell.get("track_id") or cell.get("id")), "source":source,
            "observed_at":t1.isoformat(),"lat":cell["lat"],"lon":cell["lon"],
            "speed_kmh":speed,"direction_deg":bearing(a,b),"interval_min":dt,
            "track_points":[a,b],"max_dbz":cell.get("max_dbz"),
            "predictive":cell.get("vectors") if source=="iride" else None}

def main():
    now = datetime.now(timezone.utc)
    vectors = []
    sources = {}
    times = []
    for source in ("argo","iride"):
        try:
            if source=="argo":
                raw,history = fetch("argo.json"),fetch("argo_history.json")
                observed = timestamp(raw["timestamp"])
            else:
                raw = load_local(DATA/"latest_observation.json")
                observed = timestamp(raw["observed_at"])
            if not -timedelta(minutes=10)<=now-observed<=MAX_AGE:
                raise ValueError("stale observation")
            cells = raw["cells"]
            if not isinstance(cells,list):
                raise ValueError("invalid cells")
            accepted = []
            for c in cells:
                try:
                    if source=="argo":
                        if c.get("motion",{}).get("type")!="observed":
                            continue
                        obs = history["tracks"].get(c.get("track_id") or c.get("id"),{}).get("observations",[])
                    else:
                        if not c.get("vectors",{}).get("real"):
                            continue
                        obs = c.get("track",{}).get("observations",[])
                    v = normalize(c,obs,observed,source)
                    if v:
                        accepted.append(v)
                except (ValueError,KeyError,TypeError,AttributeError):
                    continue
            vectors.extend(accepted)
            times.append(observed)
            sources[source]={"available":True,"cell_count":len(cells),"validated_vectors":len(accepted),"observed_at":observed.isoformat()}
        except Exception as exc:
            sources[source]={"available":False,"error":str(exc),"validated_vectors":0}
    pairs=[]
    for i,a in enumerate(vectors):
        if a["source"]!="iride": continue
        for j,b in enumerate(vectors):
            if b["source"]!="argo": continue
            if abs((timestamp(a["observed_at"])-timestamp(b["observed_at"])).total_seconds())>600: continue
            d=distance_km(a,b)
            turn=abs((a["direction_deg"]-b["direction_deg"]+180)%360-180)
            if d<=20 and turn<=45 and abs(a["speed_kmh"]-b["speed_kmh"])<=25:
                pairs.append((d,i,j))
    used=set()
    fused=[]
    for d,i,j in sorted(pairs):
        if i in used or j in used: continue
        used.update((i,j))
        fused.append({**vectors[i],"corroborating_track":vectors[j],"match_distance_km":d,"match_status":"candidate"})
    fused.extend(v for i,v in enumerate(vectors) if i not in used)
    atomic(DATA/"aligned_inputs.json",{"product":"SIBILLA_ALIGNED_INPUTS","schema_version":2,
        "generated_at":now.isoformat(),"observed_at":max(times).isoformat() if times else now.isoformat(),
        **sources,"vectors":fused,"raw_vector_count":len(vectors),"candidate_pairs":len(used)//2,
        "forecast_assimilation":False})
    if not times: raise RuntimeError("Both radar sources unavailable")

if __name__=="__main__":
    main()
