#!/usr/bin/env python3
"""S.I.B.I.L.L.A. - indice convettivo sperimentale GFS 0.25 gradi.
Non e' una probabilita' calibrata, ne' un sistema di allerta.
"""
import json
import math
import os
import tempfile
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'forecast'
HOURS = (3, 24, 48, 72)
BASE = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl'
NOW = datetime.now(timezone.utc)
REGION = (-5.0, 38.0, 30.0, 48.0)  # W E S N


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cycles():
    start = NOW.replace(hour=(NOW.hour // 6) * 6,
                        minute=0, second=0, microsecond=0)
    # Ultimo ciclo spesso non e' ancora pubblicato: partire dal precedente.
    for back in (1, 2, 3):
        yield start - timedelta(hours=6 * back)


def forecast_hour(cycle, horizon):
    elapsed = (NOW - cycle).total_seconds() / 3600
    lead = int(math.ceil((elapsed + horizon) / 3) * 3)
    return max(3, min(120, lead))


def url_for(cycle, lead):
    date = cycle.strftime('%Y%m%d')
    hour = cycle.strftime('%H')
    params = {
        'file': f'gfs.t{hour}z.pgrb2.0p25.f{lead:03d}',
        'dir': f'/gfs.{date}/{hour}/atmos',
        'var_CAPE': 'on', 'var_CIN': 'on', 'var_PWAT': 'on',
        'lev_surface': 'on', 'lev_entire_atmosphere': 'on',
        'subregion': '',
        'leftlon': str(REGION[0]), 'rightlon': str(REGION[1]),
        'bottomlat': str(REGION[2]), 'toplat': str(REGION[3]),
    }
    return BASE + '?' + urllib.parse.urlencode(params)


def download(cycle, lead):
    req = urllib.request.Request(
        url_for(cycle, lead),
        headers={'User-Agent': 'SIBILLA-citizen-science/1.0',
                 'Accept': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=75) as response:
        payload = response.read(16_000_001)
    if len(payload) > 16_000_000 or not payload.startswith(b'GRIB'):
        raise ValueError('Risposta NOAA non GRIB2 valida')
    return payload


def decode(payload):
    fields = {}
    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.seek(0)
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                short = str(codes_get(gid, 'shortName')).lower()
                if short not in ('cape', 'cin', 'pwat'):
                    continue
                level = str(codes_get(gid, 'typeOfLevel'))
                values = codes_get_array(gid, 'values')
                lats = codes_get_array(gid, 'latitudes')
                lons = codes_get_array(gid, 'longitudes')
                if not (len(values) == len(lats) == len(lons)):
                    continue
                # Preferenza per CAPE/CIN superficiali e PWAT colonnare.
                priority = (2 if level == 'surface' else 1) if short != 'pwat' else (2 if level == 'entireAtmosphere' else 1)
                if short not in fields or priority > fields[short][0]:
                    fields[short] = (priority, values, lats, lons)
            finally:
                codes_release(gid)
    if not all(name in fields for name in ('cape', 'cin', 'pwat')):
        raise ValueError('NOAA non contiene tutti i campi CAPE/CIN/PWAT richiesti')
    return fields


def aggregate(fields):
    buckets = defaultdict(lambda: {'cape': [], 'cin': [], 'pwat': []})
    west, east, south, north = REGION
    for name, (_, values, lats, lons) in fields.items():
        for value, lat, lon in zip(values, lats, lons):
            value, lat, lon = float(value), float(lat), float(lon)
            if lon > 180:
                lon -= 360
            if not (west <= lon < east and south <= lat < north):
                continue
            if not math.isfinite(value) or abs(value) > 1e8:
                continue
            x, y = math.floor(lon), math.floor(lat)
            buckets[(x, y)][name].append(value)
    return buckets


def grade(cape, cin, pwat):
    # Soglie euristiche: indice di potenziale, NON probabilita'.
    if cape < 100 or cin < -250 or pwat < 10:
        return 'D'
    if cape >= 2000 and cin >= -75 and pwat >= 30:
        return 'A'
    if cape >= 1000 and cin >= -125 and pwat >= 22:
        return 'B'
    if cape >= 250 and cin >= -200 and pwat >= 15:
        return 'C'
    return 'D'


def geojson(fields, cycle, lead, horizon):
    features = []
    for (x, y), item in sorted(aggregate(fields).items()):
        if not all(item[key] for key in ('cape', 'cin', 'pwat')):
            continue
        cape = max(item['cape'])
        cin = max(item['cin'])  # CIN meno negativo = minore inibizione
        pwat = sum(item['pwat']) / len(item['pwat'])
        category = grade(cape, cin, pwat)
        if category == 'D':
            continue  # Fondo neutro; non creare migliaia di poligoni D.
        coords = [[[x, y], [x + 1, y], [x + 1, y + 1],
                   [x, y + 1], [x, y]]]
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Polygon', 'coordinates': coords},
            'properties': {
                'class': category, 'category': category,
                'cape_j_kg': round(cape),
                'cin_j_kg': round(cin),
                'pwat_kg_m2': round(pwat, 1),
                'probability_calibrated': False,
                'observed': False,
            }
        })
    return {
        'type': 'FeatureCollection', 'features': features,
        'metadata': {
            'product': 'SIBILLA', 'status': 'ready',
            'model': 'NOAA GFS 0.25 degree',
            'index_type': 'experimental_convective_potential',
            'forecast_available': True,
            'probability_calibrated': False,
            'horizon_hours': horizon,
            'model_cycle': iso(cycle),
            'forecast_valid_at': iso(cycle + timedelta(hours=lead)),
            'generated_at': iso(NOW),
            'source': 'NOAA NOMADS GFS GRIB2',
            'geometry_resolution_degrees': 1,
            'note': 'Indice euristico, non probabilita e non allerta',
        }
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    for horizon in HOURS:
        error = None
        for cycle in cycles():
            lead = forecast_hour(cycle, horizon)
            try:
                payload = download(cycle, lead)
                fields = decode(payload)
                product = geojson(fields, cycle, lead, horizon)
                atomic_json(OUT / f'{horizon}h.geojson', product)
                results[str(horizon)] = {
                    'status': 'ready', 'features': len(product['features']),
                    'valid_at': product['metadata']['forecast_valid_at']}
                print(f'GFS +{horizon}h: {len(product["features"])} poligoni')
                break
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                print(f'GFS +{horizon}h ciclo {iso(cycle)}: {error}')
        else:
            results[str(horizon)] = {'status': 'unavailable', 'error': error}
    atomic_json(ROOT / 'data' / 'gfs_status.json', {
        'generated_at': iso(NOW), 'engine': 'experimental_gfs',
        'horizons': results,
    })
    if not any(x['status'] == 'ready' for x in results.values()):
        raise RuntimeError('Nessuna scadenza GFS disponibile')


if __name__ == '__main__':
    main()
              
