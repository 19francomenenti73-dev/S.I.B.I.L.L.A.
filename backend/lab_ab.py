"""Independent paired experiment. Never writes production files."""
import json, math, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone
from shapely.geometry import shape, Point
from shapely.validation import make_valid
from shapely.ops import unary_union, transform
from pyproj import Transformer

src, out = map(Path, sys.argv[1:3])
out.mkdir(parents=True, exist_ok=True)
def read(p): return json.loads(p.read_text())
def write(p,x): p.write_text(json.dumps(x, separators=(",",":"), allow_nan=False))
def dt(x): return datetime.fromisoformat(x.replace("Z","+00:00"))
now=datetime.now(timezone.utc)
project=Transformer.from_crs(4326,3035,always_xy=True).transform
def polygon_parts(g):
    if g.is_empty: return []
    if g.geom_type == "Polygon": return [g]
    if hasattr(g, "geoms"):
        return [p for part in g.geoms for p in polygon_parts(part)]
    return []

def diagnostic_union(features):
    parts=[]
    repaired=0
    collapsed=0
    for f in features:
        g=shape(f["geometry"])
        if not g.is_valid:
            g=make_valid(g)
            repaired+=1
        polygons=polygon_parts(g)
        if not polygons: collapsed+=1
        parts.extend(polygons)
    g=unary_union(parts)
    if not g.is_valid:
        g=unary_union(polygon_parts(make_valid(g)))
    return g, repaired, collapsed

for h in (4,8,16,24,36):
    a=src/"forecast"/f"{h}h.geojson"
    b=src/"experimental_forecast"/f"{h}h.geojson"
    if not a.exists() or not b.exists(): continue
    A,B=read(a),read(b)
    m=A["metadata"]
    if any(m.get(k)!=B["metadata"].get(k) for k in ("forecast_valid_at","selected_model","selection_generated_at")): continue
    issued=dt(m["selection_generated_at"])
    valid=dt(m["forecast_valid_at"])
    if not issued<=now<valid: continue
    key=hashlib.sha256(a.read_bytes()+B["metadata"].get("lab_variant","legacy").encode()).hexdigest()[:20]
    p=out/f"{h}h_{key}.json"
    if not p.exists():
        write(p,{"horizon":h,"issued":issued.isoformat(),"valid":valid.isoformat(),"captured":now.isoformat(),"A":A,"B":B})
obs=[]
for p in (src/"observations").glob("*.json"):
    try:
        o=read(p); obs.append((dt(o["observed_at"]),o))
    except (ValueError,KeyError): continue
rows=[]
archives=[]
for p in sorted(out.glob("*h_*.json")):
    if p.name.startswith("result_"):
        continue
    d=read(p)
    if not all(k in d for k in ("issued","valid","horizon","A","B")) or not all(isinstance(d[k],dict) and "features" in d[k] for k in ("A","B")):
        print("Skipping non-forecast archive:", p.name)
        continue
    valid=dt(d["valid"])
    archives.append({"id":p.stem,"horizon":d["horizon"],"valid":d["valid"],"issued":d["issued"],"added":d["B"]["metadata"].get("radar_added_polygons",0),"variant":d["B"]["metadata"].get("lab_variant","legacy")})
    report=out/("result_"+p.name)
    if report.exists(): rows.append(read(report)); continue
    if (now-valid).total_seconds()<1800 or not obs: continue
    t,o=min(obs,key=lambda x:abs((x[0]-valid).total_seconds()))
    if abs((t-valid).total_seconds())>1800: continue
    pts=[Point(c["lon"],c["lat"]) for c in o["cells"] if isinstance(c.get("lon"),(int,float)) and isinstance(c.get("lat"),(int,float)) and math.isfinite(c["lon"]) and math.isfinite(c["lat"])]
    row={"id":p.stem,"horizon":d["horizon"],"valid":d["valid"],"observed":t.isoformat(),"n":len(pts),"variant":d["B"]["metadata"].get("lab_variant","legacy")}
    for k in ("A","B"):
        g,repaired,collapsed=diagnostic_union(d[k]["features"])
        if repaired or collapsed: print(p.name,k,"repaired:",repaired,"without polygon area:",collapsed)
        pg=transform(project,g)
        distances=[pg.distance(transform(project,x))/1000 for x in pts] if not g.is_empty else []
        row[k]={"inside":sum(g.covers(x) for x in pts),"area_km2":pg.area/1e6,"mean_km":sum(distances)/len(distances) if distances else None,"geometry_repaired":repaired,"geometry_collapsed":collapsed}
    write(report,row); rows.append(row)
write(out/"summary.json",{"generated_at":now.isoformat(),"pairs":rows,"archives":archives,"note":"Paired centroid diagnostics; area expansion is not accuracy. T sample within 30 min."})
