"""LAB B v2: independent weighted spatial ensemble and observed-motion cones.
Experimental ordinal scores, not probabilities. No production writes.
"""
import json, sys, math
from pathlib import Path
from datetime import datetime, timezone
from shapely.geometry import Point, shape, mapping
from shapely.ops import unary_union, polygonize, transform
from shapely import make_valid
from pyproj import Geod, Transformer

root=Path(sys.argv[1])
out=root/"experimental_forecast"
out.mkdir(exist_ok=True)
now=datetime.now(timezone.utc)
weights={4:(60,25,10,5),8:(30,50,15,5),16:(10,70,15,5),24:(0,80,15,5),36:(0,85,10,5)}
mw={"ecmwf":40,"gfs":30,"icon":30}
geod=Geod(ellps="WGS84")
forward=Transformer.from_crs(4326,3035,always_xy=True).transform
back=Transformer.from_crs(3035,4326,always_xy=True).transform
def read(p): return json.loads(p.read_text())
def dt(s): return datetime.fromisoformat(s.replace("Z","+00:00"))
def polys(g):
    if g.geom_type=="Polygon":
        if not g.is_empty: yield g
    elif hasattr(g,"geoms"):
        for x in g.geoms: yield from polys(x)
def union(gs): return unary_union(list(gs))
def feature(g,props): return {"type":"Feature","geometry":mapping(g),"properties":props}
try:
    aligned=read(root/"aligned_inputs.json")
    vectors=aligned["vectors"]
except (OSError,ValueError,KeyError): vectors=[]
tracks=[]
for v in vectors:
    try:
        t=dt(v["observed_at"])
        lon,lat,speed,bearing=map(float,(v["lon"],v["lat"],v["speed_kmh"],v["direction_deg"]))
        if not all(map(math.isfinite,(lon,lat,speed,bearing))) or not 0<=speed<=200 or not 0<=(now-t).total_seconds()<=5400: continue
        if not -180<=lon<=180 or not -90<=lat<=90: continue
        tracks.append((v,t,lon,lat,speed,bearing))
    except (KeyError,ValueError,TypeError): continue
def disk(track,h):
    v,t,lon,lat,speed,bearing=track
    x,y,_=geod.fwd(lon,lat,bearing,speed*h*1000)
    # Uncalibrated radius: initial 10 km plus 15 km/h.
    return transform(forward,Point(x,y)).buffer(10000+15000*h)
cones=[]
for track in tracks:
    v,t,*_=track
    disks=[disk(track,i/4) for i in range(17)]
    cone=union([union([a,b]).convex_hull for a,b in zip(disks,disks[1:])])
    cones.append(feature(transform(back,cone),{"track_id":v["id"],"source":v["source"],"observed_at":t.isoformat(),"hours":4,"experimental":True}))
for h,w in weights.items():
    try: A=read(root/"forecast"/f"{h}h.geojson")
    except (OSError,ValueError): continue
    meta=A["metadata"]; valid=dt(meta["forecast_valid_at"])
    if valid<=now: continue
    models={}
    for name in mw:
        try:
            p=read(root/"models"/name/f"{h}h.geojson"); m=p["metadata"]
            if m["status"]!="ready" or not m["land_mask"] or abs((dt(m["forecast_valid_at"])-valid).total_seconds())>1800 or not -900<=(now-dt(m["generated_at"])).total_seconds()<=43200: continue
            models[name]={c:union(g for f in p["features"] if f["properties"]["class"]==c for g in polys(make_valid(transform(forward,shape(f["geometry"]))))) for c in "ABC"}
        except (OSError,ValueError,KeyError,TypeError): continue
    if not models: continue
    support=union(g for m in models.values() for g in m.values())
    # Land-clipped model support is the conservative domain until independent land mask is installed.
    endpoint=union(disk(tr,(valid-tr[1]).total_seconds()/3600) for tr in tracks if 0<=(valid-tr[1]).total_seconds()<=14400) if h==4 else union([])
    endpoint=endpoint.intersection(support)
    cw=w[0] if h==4 and not endpoint.is_empty else 0
    nw=w[1]; total=cw+nw
    boundaries=[g.boundary for m in models.values() for g in m.values() if not g.is_empty]
    if not endpoint.is_empty: boundaries.append(endpoint.boundary)
    groups={c:[] for c in "ABC"}
    for cell in polygonize(union(boundaries)):
        p=cell.representative_point()
        if not support.covers(p): continue
        numeric=sum(mw[n]*max((s for c,s in zip("ABC",(1,.66,.33)) if m[c].covers(p)),default=0) for n,m in models.items())/sum(mw[n] for n in models)
        score=(nw*numeric+cw*int(endpoint.covers(p)))/total
        if score>=.2: groups["A" if score>=.75 else "B" if score>=.45 else "C"].append(cell)
    features=[feature(transform(back,g),{"class":c,"experimental":True}) for c,gs in groups.items() for g in polys(union(gs))]
    metadata=dict(meta,lab_variant="B_v2_stage1",probability_calibrated=False,requested_weights=list(w),effective_weights={"motion":cw/total,"models":nw/total,"sinottica":0,"orography":0},model_weights={n:mw[n]/sum(mw[k] for k in models) for n in models},motion_endpoint_available=not endpoint.is_empty,radar_added_polygons=0,limitations=["Upstream validated tracking; no new TITAN association","Model support domain only","Synoptic and orographic scores not implemented","Ordinal class thresholds provisional","Motion context beyond 4h not implemented"])
    product={"type":"FeatureCollection","features":features,"metadata":metadata,"cones":{"type":"FeatureCollection","features":cones if h==4 else []}}
    (out/f"{h}h.geojson").write_text(json.dumps(product,separators=(",",":"),allow_nan=False))
