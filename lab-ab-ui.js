(()=>{"use strict";
const panel=document.getElementById("abPanel"), status=document.getElementById("abStatus"), results=document.getElementById("abResults");
const original=[...document.querySelectorAll("main > section, main > details")].filter(x=>x!==panel);
let mode="A", horizon=4, token=0;
const url="https://raw.githubusercontent.com/19francomenenti73-dev/S.I.B.I.L.L.A./lab-ab-motion/data/lab_ab/summary.json";
const fmt=(n,s="")=>Number.isFinite(n)?n.toLocaleString("it-IT",{maximumFractionDigits:1})+s:"—";
async function render(){
 const t=++token;results.replaceChildren();status.textContent="Caricamento archivio sperimentale…";
 try{
 const response=await fetch(url,{cache:"no-store"});if(!response.ok)throw Error("HTTP "+response.status);
 const d=await response.json();if(t!==token)return;
 const rows=(d.pairs||[]).filter(x=>x.horizon===horizon).sort((a,b)=>Date.parse(b.valid)-Date.parse(a.valid));
 status.textContent="+"+horizon+"h · "+rows.length+" verifiche appaiate · "+new Date(d.generated_at).toLocaleString("it-IT");
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