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
ARGO = "https://19francomenenti73-dev.github.io/A.R.G.O./data/"
MAX_AGE = timedelta(minutes=90)

def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.astimezone(timezone.utc)

def fetch(name):
    req = urllib.request.Request(ARGO + name, headers={"User-Agent": "SIBILLA-ARGO/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("ARGO payload too large")
    return json.loads(raw)

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

def main():
    now = datetime.now(timezone.utc)
    raw = fetch("argo.json")
    history = fetch("argo_history.json")
    observed = timestamp(raw["timestamp"])
    if not -timedelta(minutes=10) <= now - observed <= MAX_AGE:
        raise ValueError("ARGO observation stale or in future")
    if not isinstance(raw.get("cells"), list) or not isinstance(history.get("tracks"), dict):
        raise ValueError("Invalid ARGO schema")
    iride = load_local(DATA / "latest_observation.json")
    iride_cells = []
    if isinstance(iride, dict):
        try:
            it = timestamp(iride["observed_at"])
            if abs((it - observed).total_seconds()) <= 3600 and now - it <= MAX_AGE:
                iride_cells = iride.get("cells", [])
        except (ValueError, TypeError, KeyError):
            pass
    aligned = []
    for cell in raw["cells"]:
        if not coordinates(cell):
            continue
        motion = cell.get("motion")
        if not isinstance(motion, dict) or motion.get("type") != "observed":
            continue
        speed, direction, interval = (motion.get(k) for k in ("speed_kmh", "direction_deg", "interval_min"))
        if not all(finite(v) for v in (speed, direction, interval)) or not (0 <= speed <= 130 and 0 <= direction < 360 and 4 <= interval <= 30):
            continue
        track = history["tracks"].get(cell.get("track_id") or cell.get("id"), {})
        obs = track.get("observations", []) if isinstance(track, dict) else []
        if len(obs) < 2 or not coordinates(obs[-1]) or not coordinates(obs[-2]):
            continue
        try:
            t0, t1 = timestamp(obs[-2]["timestamp"]), timestamp(obs[-1]["timestamp"])
        except (ValueError, KeyError, TypeError):
            continue
        dt = (t1 - t0).total_seconds() / 60
        if not 4 <= dt <= 30 or abs((t1 - observed).total_seconds()) > 1800:
            continue
        if distance_km(obs[-1], cell) > 2 or abs(distance_km(obs[-2], obs[-1]) * 60 / dt - speed) > 8:
            continue
        matches = sorted(
            ((distance_km(cell, other), other.get("id")) for other in iride_cells if coordinates(other)),
            key=lambda v: v[0]
        )
        match = matches[0] if matches and matches[0][0] <= 20 else None
        aligned.append({
            "argo_id": str(cell.get("id", ""))[:100],
            "observed_at": t1.isoformat(),
            "lat": cell["lat"], "lon": cell["lon"],
            "speed_kmh": speed, "direction_deg": direction,
            "interval_min": dt,
            "track_points": [obs[-2], obs[-1]],
            "iride_match": {"id": match[1], "distance_km": round(match[0], 2)} if match else None,
            "duplicate_observation": match is not None
        })
    syn = load_local(DATA / "sinottica_intake.json")
    frames = syn.get("frames", []) if isinstance(syn, dict) else []
    syn_valid = isinstance(syn, dict) and syn.get("product") == "SIBILLA_SINOTTICA_DIAGNOSTIC"
    result = {
        "schema_version": 1, "product": "SIBILLA_ALIGNED_INPUTS",
        "generated_at": now.isoformat(), "observed_at": observed.isoformat(),
        "horizons_hours": [4, 8, 16, 24, 36],
        "argo": {"source": "RainViewer", "cell_count": len(raw["cells"]), "validated_vectors": len(aligned)},
        "iride": {"available": bool(iride_cells), "cell_count": len(iride_cells)},
        "sinottica": {"available": syn_valid, "frame_count": len(frames), "type": "model_diagnostics"},
        "vectors": aligned,
        "forecast_assimilation": False,
        "note": "Aligned observed vectors and model diagnostics; proximity is not proof of same storm. No forecast modification."
    }
    atomic(DATA / "aligned_inputs.json", result)
    print("ARGO aligned vectors:", len(aligned), "IRIDE matches:", sum(v["duplicate_observation"] for v in aligned))

if __name__ == "__main__":
    main()
