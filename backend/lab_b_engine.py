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
try:
    syn=read(root/"sinottica_intake.json")
    frames=syn["frames"] if 0<=(now-dt(syn["acquired_utc"])).total_seconds()<=5400 and 0<=(now-dt(syn["model_generated_utc"])).total_seconds()<=43200 else []
except (OSError,ValueError,KeyError,TypeError): frames=[]
def wind(grid,lon,lat):
    if not grid: return None
    ys,xs=grid["lat"],grid["lon"]
    if not min(xs)<=lon<=max(xs) or not min(ys)<=lat<=max(ys): return None
    i=min(range(len(ys)),key=lambda i:abs(ys[i]-lat))
    j=min(range(len(xs)),key=lambda j:abs(xs[j]-lon))
    return grid["u"][i][j],grid["v"][i][j]
def terrain():
    import urllib.request, urllib.parse, tempfile
    from datetime import timedelta
    try:
        import eccodes as ec
        run=now-timedelta(hours=8)
        cycle=run.hour//6*6
        query=urllib.parse.urlencode({"file":f"gfs.t{cycle:02d}z.pgrb2.0p25.f000","var_HGT":"on","lev_surface":"on","subregion":"","leftlon":-15,"rightlon":45,"toplat":55,"bottomlat":25,"dir":f"/gfs.{run:%Y%m%d}/{cycle:02d}/atmos"})
        url="https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?"+query
        with urllib.request.urlopen(url,timeout=35) as response: raw=response.read(8000001)
        if len(raw)>8000000 or not raw.startswith(b"GRIB"): return {}
        with tempfile.TemporaryFile() as f:
            f.write(raw); f.seek(0)
            g=ec.codes_grib_new_from_file(f)
            if g is None: return {}
            try:
                if ec.codes_get(g,"typeOfLevel")!="surface": return {}
                return {(round(float(y),2),round((float(x)+180)%360-180,2)):float(z) for y,x,z in zip(ec.codes_get_array(g,"latitudes"),ec.codes_get_array(g,"longitudes"),ec.codes_get_values(g)) if math.isfinite(z) and -500<=z<=9000}
            finally: ec.codes_release(g)
    except Exception as exc:
        print("Terrain unavailable:",str(exc)[:200]); return {}
heights=terrain()
def oro_score(frame,p):
    if not frame or not heights: return None
    q=transform(back,p)
    y=round(q.y*4)/4; x=round(q.x*4)/4
    uv=wind(frame.get("wind_10m"),q.x,q.y)
    if uv is None: return None
    try:
        dx=(heights[y,x+.25]-heights[y,x-.25])/(55660*math.cos(math.radians(y)))
        dy=(heights[y+.25,x]-heights[y-.25,x])/55660
    except KeyError: return None
    return min(1,max(0,uv[0]*dx+uv[1]*dy)/.3)
def syn_score(frame,p):
    if not frame: return None
    q=transform(back,p)
    a=wind(frame.get("jet_250hpa"),q.x,q.y)
    b=wind(frame.get("wind_10m"),q.x,q.y)
    if a is None or b is None: return None
    return min(1,math.hypot(a[0]-b[0],a[1]-b[1])/40)

for v in vectors:
    try:
        t=dt(v["observed_at"])
        lon,lat,speed,bearing=map(float,(v["lon"],v["lat"],v["speed_kmh"],v["direction_deg"]))
        if not all(map(math.isfinite,(lon,lat,speed,bearing))) or not 0<=speed<=200 or not 0<=(now-t).total_seconds()<=5400: continue
        if not -180<=lon<=180 or not -90<=lat<=90: continue
        points=sorted(v.get("track_points",[]),key=lambda p:dt(p["timestamp"]))
        segments=[]
        for a,b in zip(points,points[1:]):
            hours=(dt(b["timestamp"])-dt(a["timestamp"])).total_seconds()/3600
            if not 0<hours<=.75: continue
            az,_,dist=geod.inv(a["lon"],a["lat"],b["lon"],b["lat"])
            s=dist/1000/hours
            if 0<=s<=200: segments.append((s*math.sin(math.radians(az)),s*math.cos(math.radians(az)),hours))
        if segments:
            east=sum(x*z for x,y,z in segments)/sum(z for x,y,z in segments)
            north=sum(y*z for x,y,z in segments)/sum(z for x,y,z in segments)
            speed=math.hypot(east,north)
            bearing=math.degrees(math.atan2(east,north))%360
        tracks.append((v,t,lon,lat,speed,bearing))
    except (KeyError,ValueError,TypeError): continue
def disk(track,h):
    v,t,lon,lat,speed,bearing=track
    x,y,_=geod.fwd(lon,lat,bearing,speed*h*1000)
    # Uncalibrated radius: initial 10 km plus 15 km/h.
    return transform(forward,Point(x,y)).buffer(10000+15000*h)
# Candidate associations between synchronous observed centroid pairs.
# Proximity alone cannot establish physical merging or splitting.
events=[]
pairs=[]
for tr in tracks:
    v,t,*_=tr
    pts=sorted(v.get("track_points",[]),key=lambda p:dt(p["timestamp"]))
    if len(pts)>=2:
        a,b=pts[-2:]
        if 0<(dt(b["timestamp"])-dt(a["timestamp"])).total_seconds()<=2700:
            pairs.append((v,a,b))
for i,(v,a,b) in enumerate(pairs):
    for u,c,d in pairs[i+1:]:
        if v["source"]!=u["source"] or v["id"]==u["id"]: continue
        if abs((dt(a["timestamp"])-dt(c["timestamp"])).total_seconds())>60 or abs((dt(b["timestamp"])-dt(d["timestamp"])).total_seconds())>60: continue
        _,_,before=geod.inv(a["lon"],a["lat"],c["lon"],c["lat"])
        _,_,after=geod.inv(b["lon"],b["lat"],d["lon"],d["lat"])
        kind="merge_candidate" if before>=20000 and after<=10000 else "split_candidate" if before<=10000 and after>=20000 else None
        if kind:
            events.append({"type":kind,"track_ids":[v["id"],u["id"]],"source":v["source"],"observed_at":b["timestamp"],"before_km":before/1000,"after_km":after/1000,"confirmed":False})
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
    endpoint=union(disk(tr,min(4,(valid-tr[1]).total_seconds()/3600)) for tr in tracks if 0<=(valid-tr[1]).total_seconds()<=21600) if h==4 else union([])
    endpoint=endpoint.intersection(support)
    ages=[(valid-tr[1]).total_seconds()/3600 for tr in tracks if 0<=(valid-tr[1]).total_seconds()<=21600]
    # Beyond four hours the endpoint is context, not a predicted position at validity.
    decay=max(0,1-max(0,min(ages,default=6)-4)/2)
    if h in (8,16):
        # Historical observed-motion corridors, not future cell positions.
        endpoint=union(disk(tr,k/2) for tr in tracks for k in range(9)).intersection(support)
        decay=1.0
    cw=w[0]*decay if h in (4,8,16) and not endpoint.is_empty else 0
    nw=w[1]
    compatible=[f for f in frames if abs((dt(f["valid_utc"])-valid).total_seconds())<=5400 and f.get("jet_250hpa") and f.get("wind_10m")]
    frame=min(compatible,key=lambda f:abs((dt(f["valid_utc"])-valid).total_seconds())) if compatible else None
    sw=w[2] if frame else 0
    ow=w[3] if heights and frame else 0
    total=cw+nw+sw+ow
    oro_used=0
    syn_used=0
    syn_missing=0
    boundaries=[g.boundary for m in models.values() for g in m.values() if not g.is_empty]
    if not endpoint.is_empty: boundaries.append(endpoint.boundary)
    groups={c:[] for c in "ABC"}
    for cell in polygonize(union(boundaries)):
        p=cell.representative_point()
        if not support.covers(p): continue
        numeric=sum(mw[n]*max((s for c,s in zip("ABC",(1,.66,.33)) if m[c].covers(p)),default=0) for n,m in models.items())/sum(mw[n] for n in models)
        ss=syn_score(frame,p)
        local_sw=sw if ss is not None else 0
        syn_used+=int(ss is not None)
        syn_missing+=int(ss is None)
        os=oro_score(frame,p) if ow else None
        local_ow=ow if os is not None else 0
        oro_used+=int(os is not None)
        score=(nw*numeric+cw*int(endpoint.covers(p))+local_sw*(ss or 0)+local_ow*(os or 0))/(nw+cw+local_sw+local_ow)
        if score>=.2: groups["A" if score>=.75 else "B" if score>=.45 else "C"].append(cell)
    features=[feature(transform(back,g),{"class":c,"experimental":True}) for c,gs in groups.items() for g in polys(union(gs))]
    metadata=dict(meta,lab_variant="B_v2_stage6",probability_calibrated=False,requested_weights=list(w),effective_weights={"motion":cw/total,"models":nw/total,"sinottica":sw/total,"orography":ow/total},orographic_cells_used=oro_used,terrain_grid_points=len(heights),terrain_source="NOAA NCEP GFS surface HGT 0.25 degree",synoptic_cells_used=syn_used,synoptic_cells_missing=syn_missing,synoptic_valid_at=frame["valid_utc"] if frame else None,model_weights={n:mw[n]/sum(mw[k] for k in models) for n in models},motion_endpoint_available=not endpoint.is_empty,motion_context_decay=decay,motion_context_mode="bounded_endpoint" if h==4 else "observed_motion_corridor" if h in (8,16) else "none",motion_context_max_hours=4,motion_context_validity_gap_hours=max(0,min(ages,default=6)-4) if h==4 else None,association_candidates=events if h==4 else [],association_pairs_count=len(pairs),radar_added_polygons=0,limitations=["Upstream association; independent pairwise motion reconstruction; geometric split/merge candidates only; no confirmed object topology","Model support domain only","Synoptic GFS wind contrast proxy, not independent or validated bulk shear; local weights vary with coverage; orographic upslope proxy on coarse GFS terrain; uncalibrated","Ordinal class thresholds provisional","Four-hour endpoint used as decaying context for validity up to six hours; not a position forecast beyond four hours"])
    product={"type":"FeatureCollection","features":features,"metadata":metadata,"cones":{"type":"FeatureCollection","features":cones if h==4 else []}}
    (out/f"{h}h.geojson").write_text(json.dumps(product,separators=(",",":"),allow_nan=False))
