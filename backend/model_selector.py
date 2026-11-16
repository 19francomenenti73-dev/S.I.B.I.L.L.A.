#!/usr/bin/env python3
"""S.I.B.I.L.L.A. - Selettore multimodello V1.

Modelli: GFS, ECMWF IFS, ICON-EU
Orizzonti: 4, 8, 16, 24, 36 ore

Non genera dati meteorologici.
Non inventa punteggi IRIDE.
Funziona anche con un solo modello.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
MODEL_DIR = DATA / "models"
OUTPUT = DATA / "forecast"
VERIFY = DATA / "verification"

MODELS = ("gfs", "ecmwf", "icon")
HORIZONS = (4, 8, 16, 24, 36)
CLASSES = ("A", "B", "C")
MIN_SAMPLES = 20
MAX_SCORE_AGE = timedelta(days=14)
MAX_PRODUCT_AGE = timedelta(hours=12)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp mancante")
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    if dt.tzinfo is None:
        raise ValueError("Timestamp senza fuso")
    return dt.astimezone(timezone.utc)


def atomic_json(path, obj):
    path.parent.mkdir(
        parents=True, exist_ok=True
    )
    fd, temp = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp"
    )
    try:
        with os.fdopen(
            fd, "w", encoding="utf-8"
        ) as stream:
            json.dump(
                obj, stream,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":")
            )
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path):
    with path.open(
        "r", encoding="utf-8"
    ) as stream:
        return json.load(stream)


def valid_coordinates(obj):
    if not isinstance(obj, (list, tuple)):
        return False
    if len(obj) == 2 and all(
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
        for v in obj
    ):
        lon, lat = obj
        return -180 <= lon <= 180 and -90 <= lat <= 90
    return bool(obj) and all(
        valid_coordinates(v) for v in obj
    )


def validate_feature(feature):
    if not isinstance(feature, dict):
        return False

    geometry = feature.get("geometry")
    properties = feature.get("properties")

    if not isinstance(geometry, dict):
        return False
    if not isinstance(properties, dict):
        return False

    if geometry.get("type") not in (
        "Polygon", "MultiPolygon"
    ):
        return False

    if properties.get("class") not in CLASSES:
        return False

    coords = geometry.get("coordinates")
    return valid_coordinates(coords)


def validate_product(product, model, horizon):
    if not isinstance(product, dict):
        raise ValueError("Documento non valido")

    if product.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON non valido")

    features = product.get("features")
    if not isinstance(features, list):
        raise ValueError("Features mancanti")

    meta = product.get("metadata")
    if not isinstance(meta, dict):
        raise ValueError("Metadati mancanti")

    if meta.get("status") != "ready":
        raise ValueError("Modello non pronto")

    if meta.get("model_id") != model:
        raise ValueError("Identificativo modello errato")

    if meta.get("horizon_hours") != horizon:
        raise ValueError("Orizzonte errato")

    if meta.get("land_mask") is not True:
        raise ValueError("Maschera terrestre assente")

    if meta.get("probability_calibrated") is not False:
        raise ValueError("Calibrazione non dichiarata")

    generated = parse_time(
        meta.get("generated_at")
    )
    valid_at = parse_time(
        meta.get("forecast_valid_at")
    )

    current = utcnow()

    if generated > current + timedelta(minutes=15):
        raise ValueError("Data futura anomala")

    if current - generated > MAX_PRODUCT_AGE:
        raise ValueError("Prodotto scaduto")

    if valid_at <= current:
        raise ValueError("Previsione già scaduta")

    if not all(
        validate_feature(f) for f in features
    ):
        raise ValueError("Geometrie o classi non valide")

    return True


def load_models(horizon):
    available = {}
    unavailable = {}

    for model in MODELS:
        path = (
            MODEL_DIR / model /
            f"{horizon}h.geojson"
        )

        try:
            product = read_json(path)
            validate_product(
                product, model, horizon
            )
            available[model] = product

        except (
            OSError, ValueError,
            TypeError, KeyError
        ) as exc:
            unavailable[model] = str(exc)

    return available, unavailable


def verified_scores(horizon):
    """Legge verifiche esterne documentate.

    Non calcola automaticamente la coerenza
    radar: serve un verificatore dedicato.
    """
    path = VERIFY / "scores.json"

    try:
        report = read_json(path)
    except (OSError, ValueError):
        return {}

    if not isinstance(report, dict):
        return {}

    if report.get("status") != "verified":
        return {}

    if report.get("method") != "historical_iride":
        return {}

    try:
        updated = parse_time(
            report["generated_at"]
        )
    except (KeyError, TypeError, ValueError):
        return {}

    age = utcnow() - updated
    if age < timedelta(minutes=-15):
        return {}
    if age > MAX_SCORE_AGE:
        return {}

    horizons = report.get("horizons", {})
    if not isinstance(horizons, dict):
        return {}

    section = horizons.get(str(horizon), {})
    if not isinstance(section, dict):
        return {}

    scores = {}

    for model in MODELS:
        entry = section.get(model, {})
        if not isinstance(entry, dict):
            continue

        score = entry.get("score")
        samples = entry.get("samples")

        if not isinstance(score, (int, float)):
            continue
        if not isinstance(samples, int):
            continue
        if isinstance(score, bool):
            continue
        if isinstance(samples, bool):
            continue
        if not math.isfinite(score):
            continue
        if not 0 <= score <= 1:
            continue
        if samples < MIN_SAMPLES:
            continue

        scores[model] = {
            "score": float(score),
            "samples": samples
        }

    return scores


def choose_model(available, horizon):
    if not available:
        return None, "unavailable", {}

    scores = verified_scores(horizon)

    eligible = {
        model: scores[model]
        for model in available
        if model in scores
    }

    # Non confrontare punteggi di verifiche
    # non ancora sufficientemente documentate.
    if eligible and len(eligible) == len(available):
        selected = max(
            eligible,
            key=lambda model: (
                eligible[model]["score"],
                eligible[model]["samples"],
                -MODELS.index(model)
            )
        )
        return selected, "historical_iride", eligible

    # Ordine operativo temporaneo.
    # Non rappresenta maggiore accuratezza.
    for model in ("ecmwf", "icon", "gfs"):
        if model in available:
            return model, "provisional", eligible

    return None, "unavailable", eligible


def process_horizon(horizon):
    available, unavailable = load_models(
        horizon
    )

    selected, method, scores = choose_model(
        available, horizon
    )

    result = {
        "horizon_hours": horizon,
        "available_models": list(available),
        "unavailable_models": unavailable,
        "selected_model": selected,
        "selection_method": method,
        "verified_scores": scores
    }

    destination = OUTPUT / f"{horizon}h.geojson"

    if selected is None:
        if destination.exists():
            destination.unlink()
        result["status"] = "unavailable"
        return result

    original = available[selected]
    product = {
        "type": "FeatureCollection",
        "features": original["features"],
        "metadata": dict(original["metadata"])
    }

    product["metadata"].update({
        "product": "SIBILLA",
        "status": "ready",
        "selected_model": selected,
        "available_models": list(available),
        "selection_method": method,
        "verification_status": (
            "historically_verified"
            if method == "historical_iride"
            else "experimental_unverified"
        ),
        "probability_calibrated": False,
        "selection_generated_at": iso(utcnow())
    })

    atomic_json(destination, product)
    result["status"] = "ready"
    result["feature_count"] = len(
        product["features"]
    )

    return result


def main():
    OUTPUT.mkdir(
        parents=True, exist_ok=True
    )

    results = {}

    for horizon in HORIZONS:
        try:
            results[str(horizon)] = (
                process_horizon(horizon)
            )
        except Exception as exc:
            path = OUTPUT / f"{horizon}h.geojson"
            if path.exists():
                path.unlink()
            results[str(horizon)] = {
                "status": "error",
                "error": str(exc)
            }

    report = {
        "product": "SIBILLA_MULTIMODEL",
        "generated_at": iso(utcnow()),
        "models": list(MODELS),
        "horizons_hours": list(HORIZONS),
        "results": results
    }

    atomic_json(
        DATA / "model_selection_status.json",
        report
    )

    print(json.dumps(
        report,
        indent=2,
        ensure_ascii=False
    ))


if __name__ == "__main__":
    main()
                          
