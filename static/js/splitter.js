const byId = (id) => document.getElementById(id);
let selectedFiles = [];
let splitJobId = null;
let plans = [];
const videoInput = byId("videos");
const folderInput = byId("folder");
const message = (text, error=false) => { byId("message").textContent=text; byId("message").style.color=error?"#ef4444":""; };
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
function choose(files) {
  selectedFiles = [...files].filter(f => f.type.startsWith("video/") || /\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(f.name));
  byId("fileList").innerHTML = selectedFiles.map(f => `<div class="split-item">${f.webkitRelativePath || f.name} · ${(f.size/1048576).toFixed(1)} MB</div>`).join("") || "";
  splitJobId = null; plans=[]; byId("planTable").innerHTML=""; byId("exportBtn").disabled=true; byId("downloadLink").hidden=true;byId("stopBtn").disabled=true;
  if(selectedFiles[0])byId("splitPreviewVideo").src=URL.createObjectURL(selectedFiles[0]);
}
videoInput.addEventListener("change", e => choose(e.target.files));
folderInput.addEventListener("change", e => choose(e.target.files));
function fmt(s){const m=Math.floor(s/60),r=Math.floor(s%60);return `${String(m).padStart(2,"0")}:${String(r).padStart(2,"0")}`;}
const previewPositions={text:{x:50,y:82},label:{x:75,y:4}};
function placeSplitPreview(key){const el=byId(key==="text"?"splitTextPreview":"splitLabelPreview"),p=previewPositions[key];el.style.left=`${p.x}%`;el.style.top=`${p.y}%`;el.style.right="auto";el.style.transform="none";el.dataset.pctX=p.x;el.dataset.pctY=p.y;}
function updateSplitPreview(){const text=byId("overlayText").value,label=byId("partLabel").value;byId("splitTextPreview").textContent=text||"Text overlay";byId("splitTextPreview").style.display=text?"block":"none";byId("splitTextPreview").style.fontSize=`${Math.max(8,Number(byId("overlaySize").value)||48)}px`;byId("splitTextPreview").style.opacity=String(Math.max(0,Math.min(1,Number(byId("overlayOpacity").value)||0)));byId("splitTextPreview").style.background=byId("overlayBackground").checked?"#0009":"transparent";byId("splitLabelPreview").textContent=label.replace("{n}","1").replace("{mm}","၁").replace("{total}","3");byId("splitLabelPreview").style.display=byId("showNumber").checked?"block":"none";byId("splitPreviewStage").style.aspectRatio=({"Original":"16 / 9","16:9":"16 / 9","9:16":"9 / 16","1:1":"1 / 1"})[byId("ratio").value]||"16 / 9";}
function dragSplitLayer(key){const el=byId(key==="text"?"splitTextPreview":"splitLabelPreview");el.addEventListener("pointerdown",e=>{e.preventDefault();e.stopPropagation();el.setPointerCapture(e.pointerId);const move=ev=>{const r=byId("splitPreviewStage").getBoundingClientRect();const x=Math.max(0,Math.min(95,(ev.clientX-r.left)/r.width*100)),y=Math.max(0,Math.min(95,(ev.clientY-r.top)/r.height*100));previewPositions[key]={x,y};placeSplitPreview(key);};const end=()=>{el.removeEventListener("pointermove",move);el.removeEventListener("pointerup",end);};el.addEventListener("pointermove",move);el.addEventListener("pointerup",end,{once:true});});}
placeSplitPreview("text");placeSplitPreview("label");dragSplitLayer("text");dragSplitLayer("label");
["overlayText","partLabel","showNumber","overlaySize","overlayOpacity","overlayBackground"].forEach(id=>byId(id).addEventListener("input",updateSplitPreview));byId("ratio").addEventListener("change",updateSplitPreview);updateSplitPreview();
byId("planBtn").addEventListener("click", async () => {
  if (!selectedFiles.length) return message("Video ဖိုင်အရင်ရွေးပါ။", true);
  const button=byId("planBtn"); button.disabled=true; message("Video တင်ပြီး cut points စစ်နေပါသည်…");
  try {
    const fd=new FormData(); selectedFiles.forEach(f=>fd.append("files",f,f.name.split("/").pop()));
    const uploaded=await fetch("/api/split/upload",{method:"POST",body:fd}); const up=await uploaded.json();
    if(!uploaded.ok) throw new Error(up.detail||"Upload မအောင်မြင်ပါ"); splitJobId=up.job_id;
    const planned=await fetch(`/api/split/${splitJobId}/plan`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({part_minutes:Number(byId("partMinutes").value),smart_cut:byId("smartCut").checked,max_wait_seconds:Number(byId("maxWait").value)})});
    const data=await planned.json(); if(!planned.ok) throw new Error(data.detail||"Cut plan မရပါ"); plans=data.videos;
    byId("planTable").innerHTML=plans.map(v=>`<h3>${v.name}</h3><div class="split-item">${v.parts.map((p,i)=>`Part ${i+1}: ${fmt(p.start)} – ${fmt(p.end)} (${fmt(p.duration)})`).join(" · ")}</div>`).join("");
    byId("exportBtn").disabled=false; message(`${plans.reduce((n,v)=>n+v.parts.length,0)} parts တွက်ချက်ပြီးပါပြီ။`);
  } catch(err){message(err.message,true);} finally{button.disabled=false;}
});
function exportOptions(){return {ratio:byId("ratio").value,fit_mode:byId("fill").value,overlay_text:byId("overlayText").value,overlay_size:Number(byId("overlaySize").value),overlay_opacity:Number(byId("overlayOpacity").value),overlay_background:byId("overlayBackground").checked,overlay_placement:byId("overlayPlacement").value,overlay_x:previewPositions.text.x,overlay_y:previewPositions.text.y,show_part_number:byId("showNumber").checked,part_label:byId("partLabel").value,label_placement:byId("labelPlacement").value,label_x:previewPositions.label.x,label_y:previewPositions.label.y};}
byId("exportBtn").addEventListener("click", async()=>{
  if(!splitJobId)return;const button=byId("exportBtn");button.disabled=true;byId("stopBtn").disabled=false;byId("downloadLink").hidden=true;message("Parts တွေ render လုပ်နေပါသည်…");
  try{
    const r=await fetch(`/api/split/${splitJobId}/export`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(exportOptions())});const d=await r.json();if(!r.ok)throw new Error(d.detail||"Export failed");
    while(true){await sleep(1200);const statusResponse=await fetch(`/api/split/${splitJobId}/status`);const state=await statusResponse.json();
      message(`${state.message||state.status} · ${Number(state.progress||0).toFixed(0)}%`);
      if(state.status==="completed"){const link=byId("downloadLink");link.href=state.file_url;link.hidden=false;message(`ပြီးပါပြီ — ${state.parts_count||plans.reduce((n,v)=>n+v.parts.length,0)} parts ရပါပြီ။`);break;}
      if(state.status==="stopped"){message("လုံခြုံစွာ ရပ်နားပြီးပါပြီ။ START / RESUME နှိပ်လျှင် မပြီးသေးသောအပိုင်းမှ ဆက်လုပ်ပါမည်။");break;}
      if(state.status==="failed")throw new Error(state.message||"Export failed");
    }
  }catch(err){message(err.message,true);}finally{button.disabled=false;byId("stopBtn").disabled=true;}
});
byId("stopBtn").addEventListener("click",async()=>{if(!splitJobId)return;byId("stopBtn").disabled=true;try{await fetch(`/api/split/${splitJobId}/stop`,{method:"POST"});message("လက်ရှိ part ပြီးသွားရင် ရပ်ပါမည်…");}catch(e){message(e.message,true);}});
