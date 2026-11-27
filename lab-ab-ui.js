(()=>{"use strict";
const panel=document.getElementById("abPanel"), status=document.getElementById("abStatus"), results=document.getElementById("abResults");
const original=[...document.querySelectorAll("main > section, main > details")].filter(x=>x!==panel);
let mode="A", horizon=4, token=0, mapB=null;

const url="https://raw.githubusercontent.com/19francomenenti73-dev/S.I.B.I.L.L.A./lab-ab-motion/data/lab_ab/summary.json";
const mapBox=document.createElement("div");mapBox.style.cssText="height:clamp(160px,32vh,320px);border-radius:6px;margin:5px 0";panel.insertBefore(mapBox,results);
const picker=document.createElement("select");picker.style.width="100%";panel.insertBefore(picker,mapBox);
const note=document.createElement("p");panel.insertBefore(note,results);
async function draw(id,t){
 if(!mapB){mapB=L.map(mapBox).setView([42,12],5);L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OpenStreetMap contributors",maxZoom:19}).addTo(mapB);}
 mapB.invalidateSize();mapB.eachLayer(l=>{if(l instanceof L.GeoJSON)mapB.removeLayer(l);});
 if(!id){note.textContent="Nessuna proiezione archiviata.";return;}
 try{const q=await fetch(url.replace("summary.json",id+".json"),{cache:"no-store"});if(!q.ok)throw Error("HTTP "+q.status);const d=await q.json();if(t!==token||picker.value!==id)return;
 const group=L.featureGroup();for(const k of mode==="B"?["B"]:["A","B"]){const layer=L.geoJSON(d[k],{style:f=>({color:({A:"#e879f9",B:"#60a5fa",C:"#22d3ee"})[f.properties.class]||"#aaa",weight:k==="A"?2:3,dashArray:mode==="AB"&&k==="A"?"6 5":null,fillOpacity:k==="A"&&mode==="AB"?0:.18})}).addTo(mapB);group.addLayer(layer);}
 if(d.B.cones){const layer=L.geoJSON(d.B.cones,{style:{color:"#fbbf24",weight:1.5,dashArray:"4 4",fillOpacity:.08}}).addTo(mapB);group.addLayer(layer);}
 if(group.getBounds().isValid())mapB.fitBounds(group.getBounds(),{maxZoom:7});
 note.textContent="Validità "+new Date(d.valid).toLocaleString("it-IT",{timeZone:"Europe/Rome"})+" · "+(d.B.metadata.lab_variant||"legacy")+" · Coni: "+(d.B.cones?.features?.length||0)+(mode==="AB"?" · A tratteggiato, B continuo":"")+" · Classi A magenta, B blu, C ciano";
 }catch(e){if(t===token)note.textContent="Mappa non disponibile: "+e.message;}
}
const fmt=(n,s="")=>Number.isFinite(n)?n.toLocaleString("it-IT",{maximumFractionDigits:1})+s:"—";
async function render(){
 const t=++token;results.replaceChildren();status.textContent="Caricamento archivio sperimentale…";
 try{
 const response=await fetch(url,{cache:"no-store"});if(!response.ok)throw Error("HTTP "+response.status);
 const d=await response.json();if(t!==token)return;
 const rows=(d.pairs||[]).filter(x=>x.horizon===horizon&&x.variant==="B_v2_stage3").sort((a,b)=>Date.parse(b.valid)-Date.parse(a.valid));
 status.textContent="+"+horizon+"h · "+rows.length+" verifiche appaiate · "+new Date(d.generated_at).toLocaleString("it-IT");
 const archives=(d.archives||d.pairs||[]).filter(x=>x.horizon===horizon&&x.variant==="B_v2_stage3").sort((a,b)=>Date.parse(b.valid)-Date.parse(a.valid));picker.replaceChildren();for(const x of archives){const o=document.createElement("option");o.value=x.id;o.textContent=new Date(x.valid).toLocaleString("it-IT",{timeZone:"Europe/Rome"})+(rows.some(r=>r.id===x.id)?" · verificata":" · in attesa");picker.append(o);}picker.onchange=()=>draw(picker.value,t);draw(picker.value,t);status.textContent+=" · "+archives.length+" archivi";
 if(!rows.length){results.textContent="Nessuna verifica maturata. Non equivale a zero percento.";return;}
 const table=document.createElement("table");table.style.cssText="width:100%;text-align:left;font-size:inherit";
 const add=(cells,head=false)=>{const tr=table.insertRow();for(const v of cells){const c=document.createElement(head?"th":"td");c.textContent=v;tr.append(c);}};
 const variants=mode==="B"?["B"]:["A","B"];
 add(["Validità","Motore","Dentro","Distanza","Area km²"],true);
 for(const row of rows.slice(0,30))for(const k of variants){const m=row[k];add([new Date(row.valid).toLocaleString("it-IT"),k,fmt(row.n?100*m.inside/row.n:null,"%"),fmt(m.mean_km," km"),fmt(m.area_km2)]);}
 results.append(table);const p=document.createElement("p");p.textContent="Campione radar comune entro ±30 min. Diagnostica dei centroidi, non accuratezza complessiva.";results.append(p);
 }catch(e){if(t===token)status.textContent="Archivio B non disponibile: "+e.message;}
}
function select(m){mode=m;++token;for(const k of ["A","B","AB"])document.getElementById("mode"+k).setAttribute("aria-pressed",String(k===m));original.forEach(x=>x.hidden=m!=="A");panel.hidden=m==="A";if(m!=="A")render();else setTimeout(()=>window.dispatchEvent(new Event("resize")),0);}
for(const k of ["A","B","AB"])document.getElementById("mode"+k).onclick=()=>select(k);
for(const h of [4,8,16,24,36]){const b=document.createElement("button");b.textContent="+"+h+"h";b.setAttribute("aria-pressed",String(h===4));b.onclick=()=>{horizon=h;for(const x of b.parentNode.children)x.setAttribute("aria-pressed",String(x===b));render();};document.getElementById("abHorizons").append(b);}
})();