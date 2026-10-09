"""本地评委操作页面和菜谱目录读取。"""

from __future__ import annotations

import json
from pathlib import Path


def load_recipe_catalog(recipe_dir: Path) -> list[dict[str, str]]:
    """读取所有已解析菜谱，只向网页暴露正式接口需要的 id 和 name。"""

    recipes: list[dict[str, str]] = []
    for path in recipe_dir.glob("*.json"):
        if path.name == "failed_recipes.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        recipe_id = str(data.get("recipe_id") or path.stem).strip()
        name = str(data.get("name") or "").strip()
        if recipe_id and name:
            recipes.append({"id": recipe_id, "name": name})
    return sorted(recipes, key=lambda item: (item["name"], item["id"]))


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>食序引擎 MealSeq Engine</title>
<style>
:root{--bg:#f3f7f8;--card:#fff;--text:#102a35;--muted:#687b83;--line:#dbe5e7;--blue:#087f8c;--blue-dark:#075d68;--blue2:#e5f6f5;--green:#11845b;--red:#c2413b;--navy:#0b2b3a;--teal:#21b6a8;--amber:#f0a14a}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 8% 0,#dff5f2 0,transparent 31%),radial-gradient(circle at 94% 8%,#e2edf6 0,transparent 25%),var(--bg);color:var(--text);font-family:Inter,"Microsoft YaHei",Arial,sans-serif}
.shell{max-width:1580px;margin:auto;padding:22px}.hero{position:relative;overflow:hidden;display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:20px;padding:24px 28px;border-radius:22px;background:linear-gradient(125deg,#092838,#0d4551 58%,#087f8c);color:#fff;box-shadow:0 18px 44px #1135422e}.hero:after{content:"";position:absolute;right:-80px;bottom:-125px;width:330px;height:330px;border:55px solid #ffffff0b;border-radius:50%;pointer-events:none}
.brand-lockup{position:relative;z-index:1;display:flex;align-items:center;gap:16px}.brand-mark{display:grid;place-items:center;flex:0 0 58px;height:58px;border:1px solid #ffffff48;border-radius:17px;background:linear-gradient(145deg,#ffffff24,#ffffff0d);box-shadow:inset 0 1px #ffffff42;font-size:18px;font-weight:800;letter-spacing:.08em}.brand-eyebrow{margin-bottom:3px;color:#8ce3d9;font-size:11px;font-weight:800;letter-spacing:.18em;text-transform:uppercase}
h1{margin:0 0 6px;font-size:30px;letter-spacing:.015em}.brand-en{margin-left:8px;color:#ccefed;font-size:18px;font-weight:500;letter-spacing:.02em}.hero .sub{color:#d8ecee}.hero-side{position:relative;z-index:1;display:flex;gap:9px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.badge{padding:9px 13px;border-radius:999px;background:#ffffff16;color:#fff;border:1px solid #ffffff32;font-weight:700;white-space:nowrap}.mode-badge{background:#e9fffb;color:#075d68;border-color:#e9fffb}
.restart-button{padding:10px 15px;border:1px solid #ffd8b2;background:#fff7ed;color:#9a3412;box-shadow:0 7px 18px #071f2850;white-space:nowrap}.restart-button:hover{background:#ffedd5;transform:translateY(-1px)}
.flow{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.flow span{padding:6px 10px;border-radius:999px;background:#ffffff12;border:1px solid #ffffff21;font-size:12px;color:#e7f5f5}
.layout{display:grid;grid-template-columns:minmax(360px,410px) minmax(0,1fr);gap:20px;align-items:start}.card{min-width:0;background:linear-gradient(180deg,#ffffff,#fcfefe);border:1px solid #dfe9ea;border-radius:18px;padding:18px;box-shadow:0 10px 32px #1739450e}.control-card{position:sticky;top:14px}.result-card{overflow:hidden}
.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.card h2{font-size:18px;margin:0}.section-kicker{font-size:12px;color:var(--muted);white-space:nowrap}.toolbar{display:flex;gap:9px}.toolbar input,.manual input,.reschedule-control input{width:100%;border:1px solid var(--line);border-radius:11px;padding:11px 12px;font-size:14px;outline:none;background:#fbfcfe}.toolbar input:focus,.manual input:focus,.reschedule-control input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #246bfd16;background:#fff}
.recipe-list{height:min(34vh,330px);min-height:220px;overflow:auto;margin:12px -4px 12px 0;padding-right:6px}.recipe{display:flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:12px;padding:9px 10px;margin-bottom:7px;cursor:pointer;transition:.15s ease;background:#fff}.recipe:hover{border-color:#7accc5;background:#f4fbfa;transform:translateY(-1px);box-shadow:0 5px 14px #087f8c10}.recipe.disabled{opacity:.42;cursor:not-allowed;transform:none}.recipe input{width:17px;height:17px;accent-color:var(--blue)}.recipe-name{font-weight:700}.recipe-id{font:11px Consolas,monospace;color:var(--muted);margin-top:3px}
.manual{border-top:1px solid var(--line);padding-top:13px}.manual-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.manual button{margin-top:8px}
.selection{min-height:44px;margin:12px 0;display:flex;align-items:flex-start;gap:10px}.selection>.sub{white-space:nowrap;padding-top:7px}#chips{display:flex;flex:1;flex-wrap:wrap;gap:7px}.chip{display:inline-flex;align-items:center;gap:6px;background:var(--blue2);color:var(--blue-dark);border-radius:999px;padding:6px 8px 6px 11px;font-size:13px;line-height:1.2}.chip-remove{display:grid;place-items:center;width:19px;height:19px;padding:0;border-radius:50%;background:#c8ebe7;color:var(--blue-dark);font-size:15px;line-height:1}.chip-remove:hover{background:#a6ddd7}.count{font-weight:700;color:var(--blue)}
.reschedule-control{margin:4px 0 13px;padding:12px;border:1px solid #bde8e3;border-radius:12px;background:#effaf8}.reschedule-control label{display:block;margin-bottom:7px;font-weight:700;color:#075d68}.reschedule-control .hint{margin-top:7px;color:#55767b;font-size:12px;line-height:1.5}
.request-preview{margin:10px 0 13px}.request-preview summary{cursor:pointer;font-weight:700}.request-preview pre{max-height:170px;margin:9px 0 0}
button,.button-link{border:0;border-radius:11px;padding:11px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;transition:.15s ease}.primary{background:linear-gradient(135deg,#087f8c,#21b6a8);color:#fff;width:100%;box-shadow:0 8px 20px #087f8c2d}.primary:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 11px 25px #087f8c38}.primary:disabled{background:#aebfc2;box-shadow:none;cursor:not-allowed}.secondary{background:#edf4f4;color:#294750}.secondary:hover{background:#e0eeed}.actions{display:flex;gap:9px}.actions>*{flex:1;text-align:center}.message{margin-top:11px;padding:11px;border-radius:11px;background:#f1f6f6;color:var(--muted);line-height:1.5;font-size:13px}.message.error{background:#fff0f0;color:var(--red)}.message.ok{background:#eaf8f3;color:var(--green)}
.metrics{display:grid;grid-template-columns:repeat(3,minmax(135px,1fr));gap:10px;margin-bottom:14px}.metric{background:linear-gradient(145deg,#f7f9fc,#edf2f8);border:1px solid #edf1f6;border-radius:13px;padding:14px}.metric small{color:var(--muted)}.metric strong{display:block;font-size:22px;margin-top:7px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;font-size:13px;min-width:650px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line)}tbody tr:last-child td{border-bottom:0}th{color:var(--muted);background:#f7f9fc}
.empty{min-height:220px;display:grid;place-items:center;text-align:center;color:var(--muted);padding:24px}.restore-panel{min-height:220px;display:grid;place-items:center;text-align:center;padding:28px;border:1px dashed #9fd9d3;border-radius:14px;background:linear-gradient(145deg,#f7fcfb,#eaf8f6)}.restore-panel strong{display:block;font-size:21px;margin-bottom:9px}.restore-panel p{margin:0 auto 15px;color:var(--muted);line-height:1.7;max-width:600px}iframe{width:100%;height:610px;border:1px solid var(--line);border-radius:12px;margin-top:14px;background:#fff}.result-links{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.result-links a{background:#edf6f5;color:#294750;padding:9px 12px;border-radius:9px;text-decoration:none;font-size:13px;font-weight:700}.result-links a:hover{background:#dcefeb;color:var(--blue-dark)}
details{margin-top:13px}pre{max-height:320px;overflow:auto;background:#121722;color:#d8e2f2;padding:14px;border-radius:10px;font-size:12px}
@media(max-width:1050px){.layout{grid-template-columns:1fr}.control-card{position:static}.recipe-list{height:300px}.metrics{grid-template-columns:repeat(2,1fr)}}@media(max-width:620px){.shell{padding:10px}.hero{padding:18px;align-items:flex-start;flex-direction:column}.hero-side{justify-content:flex-start}.manual-grid{grid-template-columns:1fr}.metrics{grid-template-columns:1fr}.actions{flex-direction:column}}
</style>
</head>
<body><main class="shell">
<div class="hero"><div class="brand-lockup"><div class="brand-mark">MS</div><div><div class="brand-eyebrow">MEALSEQ ENGINE · 食序引擎</div><h1>食序引擎 <span class="brand-en">MealSeq Engine</span></h1><div class="sub">让每一道菜按序协作、准时出锅。</div><div class="flow"><span>1 · 选择菜谱</span><span>2 · 生成静态排程</span><span>3 · 自动识别加菜并动态重排</span></div></div></div><div class="hero-side"><div class="badge">● 调度服务在线</div><div id="mode-badge" class="badge mode-badge">等待首次排程</div><button id="new-plan" class="restart-button" type="button">重新开始 / 新一轮</button></div></div>
<div class="layout">
  <section class="card control-card">
    <div class="section-head"><h2 id="mode-title">① 选择至少 3 道菜开始排程</h2><span class="section-kicker">输入为 id + name</span></div>
    <div class="toolbar"><input id="search" placeholder="搜索菜名或菜谱 ID"></div>
    <div id="recipe-list" class="recipe-list"><div class="empty">正在读取菜谱库……</div></div>
    <div class="manual"><b>直接填写菜谱id和名字</b><div class="manual-grid"><input id="manual-id" placeholder="菜谱 ID"><input id="manual-name" placeholder="菜谱名称"></div><button id="manual-add" class="secondary">加入选择</button></div>
    <div class="selection"><span class="sub">已选择：<span id="selected-count" class="count">0</span></span><div id="chips"></div></div>
    <div id="reschedule-control" class="reschedule-control" hidden><label for="reschedule-minute">插入菜品时刻（可选）</label><input id="reschedule-minute" type="number" min="0" step="0.5" placeholder="例如 10，表示第10分钟插入"><div class="hint">填写后按当前排程开始后的指定分钟重调度；留空则按接口收到加菜请求的实际时刻重调度。</div></div>
    <details class="request-preview" open><summary>即将传入接口的原始 JSON</summary><pre id="request-json">[]</pre></details>
    <button id="submit" class="primary" disabled>生成静态排程</button>
    <div class="actions" style="margin-top:9px"><a class="button-link secondary" href="/artifacts/02_static_result.json" target="_blank">查看静态 JSON</a></div>
    <div id="message" class="message">请选择至少3道菜，不限制最多数量。首次提交是静态排程，成功后页面自动切换到“加一道菜”。</div>
  </section>
  <section class="card result-card">
    <div class="section-head"><h2>② 排程结果</h2><span class="section-kicker">指标 · 时间表 · 甘特图</span></div>
    <div id="result"><div class="empty">完成选择并提交后，这里会显示接口响应和甘特图。</div></div>
  </section>
</div></main>
<script>
const state={recipes:[],selected:new Map(),active:new Set(),hasPlan:false,lastResult:null};
const el=id=>document.getElementById(id);
const escapeHtml=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function loadRecipes(){
  const response=await fetch('/recipes');
  if(!response.ok) throw new Error('菜谱目录读取失败');
  state.recipes=await response.json(); renderRecipes();
}
async function restoreSession(){
  const response=await fetch('/session');
  if(!response.ok)throw new Error('当前排程状态读取失败');
  const session=await response.json();
  state.active=new Set((session.recipes||[]).map(x=>String(x.id)));
  setMode(Boolean(session.hasPlan));
  if(session.hasPlan){
    showMessage(`已恢复进行中的排程（版本 ${session.version}）。刷新或重新打开页面不会重置，下一次提交将加入1道菜并动态重排。`);
    renderRecoveredSession(session);
  }
}
function renderRecoveredSession(session){
  const dynamic=Number(session.version)>1;
  const chart=dynamic?'/artifacts/07_dynamic_gantt.html':'/artifacts/03_static_gantt.html';
  const result=dynamic?'/artifacts/05_dynamic_result.json':'/artifacts/02_static_result.json';
  el('result').innerHTML=`<div class="restore-panel"><div><strong>已恢复进行中的排程</strong><p>当前版本 ${escapeHtml(session.version)}，包含 ${escapeHtml((session.recipes||[]).length)} 道菜。可继续选择一道新菜动态加入；只有点击“重新开始 / 新一轮”或当前排程执行完毕后，才会进入新的静态排程。</p><div class="result-links" style="justify-content:center"><a href="${result}" target="_blank">查看当前 JSON</a><a href="${chart}" target="_blank">查看当前甘特图</a></div></div></div>`;
}
function renderRecipes(){
  const keyword=el('search').value.trim().toLowerCase();
  const items=state.recipes.filter(x=>!keyword||x.name.toLowerCase().includes(keyword)||x.id.toLowerCase().includes(keyword));
  el('recipe-list').innerHTML=items.map(item=>{
    const disabled=state.hasPlan&&state.active.has(item.id);
    return `<label class="recipe ${disabled?'disabled':''}"><input type="checkbox" data-id="${escapeHtml(item.id)}" ${state.selected.has(item.id)?'checked':''} ${disabled?'disabled':''}><div><div class="recipe-name">${escapeHtml(item.name)}</div><div class="recipe-id">${escapeHtml(item.id)}</div></div></label>`;
  }).join('')||'<div class="empty">没有匹配的菜谱</div>';
  document.querySelectorAll('[data-id]').forEach(box=>box.addEventListener('change',()=>toggleRecipe(box.dataset.id,box.checked)));
}
function toggleRecipe(id,checked){
  const item=state.recipes.find(x=>x.id===id); if(!item)return;
  if(checked)state.selected.set(id,item);else state.selected.delete(id); renderSelection();
}
function renderSelection(){
  el('selected-count').textContent=state.selected.size;
  const selectedItems=[...state.selected.values()];
  el('chips').innerHTML=selectedItems.map(x=>`<span class="chip"><span>${escapeHtml(x.name)}</span><button type="button" class="chip-remove" data-remove-id="${escapeHtml(x.id)}" title="删除${escapeHtml(x.name)}" aria-label="删除${escapeHtml(x.name)}">×</button></span>`).join('');
  document.querySelectorAll('[data-remove-id]').forEach(button=>button.addEventListener('click',()=>{
    state.selected.delete(button.dataset.removeId);renderRecipes();renderSelection();
  }));
  el('request-json').textContent=JSON.stringify(selectedItems.map(({id,name})=>({id,name})),null,2);
  const valid=state.hasPlan?state.selected.size===1:state.selected.size>=3;
  el('submit').disabled=!valid;
  el('submit').textContent=state.hasPlan?'加入这道菜并动态重排':'生成静态排程';
}
function setMode(hasPlan){
  state.hasPlan=hasPlan; state.selected.clear();
  el('mode-title').textContent=hasPlan?'① 再选择 1 道菜动态加入':'① 选择至少 3 道菜开始排程';
  el('mode-badge').textContent=hasPlan?'动态加菜模式':'等待首次排程';
  el('reschedule-control').hidden=!hasPlan;
  if(!hasPlan)el('reschedule-minute').value='';
  renderRecipes(); renderSelection();
}
function addManual(){
  const id=el('manual-id').value.trim(),name=el('manual-name').value.trim();
  if(!id||!name){showMessage('ID和名称都需要填写。',true);return;}
  if(state.hasPlan&&state.active.has(id)){showMessage('这道菜已经在当前排程中。',true);return;}
  const item={id,name}; state.selected.set(id,item);
  if(!state.recipes.some(x=>x.id===id))state.recipes.unshift(item);
  el('manual-id').value='';el('manual-name').value='';renderRecipes();renderSelection();
}
function showMessage(text,error=false){el('message').className=`message ${error?'error':'ok'}`;el('message').textContent=text;}
async function submitSchedule(){
  const items=[...state.selected.values()].map(({id,name})=>({id,name}));
  const dynamic=state.hasPlan;
  const rescheduleMinuteRaw=dynamic?el('reschedule-minute').value.trim():'';
  if(rescheduleMinuteRaw){
    const minute=Number(rescheduleMinuteRaw);
    if(!Number.isFinite(minute)||minute<0){showMessage('插入菜品时刻必须是大于或等于0的分钟数。',true);return;}
  }
  const scheduleUrl=rescheduleMinuteRaw?`/schedule?reschedule_at_min=${encodeURIComponent(rescheduleMinuteRaw)}`:'/schedule';
  el('submit').disabled=true;el('submit').textContent='正在优化排程，请稍候……';
  showMessage(rescheduleMinuteRaw?`正在按第 ${rescheduleMinuteRaw} 分钟插入新菜并重调度……`:`正在通过 POST /schedule 提交${items.length}道菜……`);
  try{
    const response=await fetch(scheduleUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(items)});
    const performance={
      runtimeMs:response.headers.get('X-Schedule-Runtime-Ms'),
      maxContinuousHumanMin:response.headers.get('X-Max-Continuous-Human-Min'),
      finishSpreadMin:response.headers.get('X-Finish-Spread-Min'),
      rescheduleAtMin:response.headers.get('X-Reschedule-At-Min')
    };
    const data=await response.json(); if(!response.ok)throw new Error(data.error||'排程失败');
    items.forEach(x=>state.active.add(x.id)); state.lastResult=data; renderResult(data,dynamic,performance);
    setMode(true);
    if(dynamic)el('reschedule-minute').value='';
    showMessage(dynamic?(rescheduleMinuteRaw?`新增菜已在第 ${rescheduleMinuteRaw} 分钟插入，动态重排完成。`:'新增菜已按接口收到请求的时刻加入，动态重排完成。'):'静态排程完成，现在可以再选择1道菜进行动态重排。');
  }catch(error){showMessage(error.message,true);renderSelection();}
}
function renderResult(data,dynamic,performance={}){
  const o=data.overview||{}, timeline=data.cookingTimeline||[];
  const chart=dynamic?'/artifacts/07_dynamic_gantt.html':'/artifacts/03_static_gantt.html';
  const runtime=Number(performance.runtimeMs);
  const runtimeText=Number.isFinite(runtime)?(runtime<1000?`${runtime.toFixed(1)} ms`:`${(runtime/1000).toFixed(2)} 秒`):'-';
  const maxHuman=performance.maxContinuousHumanMin||'-';
  const rows=timeline.map(x=>`<tr><td>${escapeHtml(x.name)}</td><td>${escapeHtml(x.product||'-')}</td><td>${escapeHtml(x.startTime)}</td><td>${escapeHtml(x.endTime)}</td><td>${escapeHtml(x.timeSpent)} 分</td></tr>`).join('');
  el('result').innerHTML=`<div class="metrics"><div class="metric"><small>菜品数</small><strong>${escapeHtml(o.recipeCount)}</strong></div><div class="metric"><small>总耗时</small><strong>${escapeHtml(o.timeSpent)} 分</strong></div><div class="metric"><small>出锅时间差</small><strong>${escapeHtml(performance.finishSpreadMin||'-')} 分</strong></div><div class="metric"><small>相对顺序制作节省</small><strong>${escapeHtml(o.timeSave||'0')} 分</strong></div>${dynamic?`<div class="metric" style="background:#fff1f2;border-color:#fecdd3"><small>重调度时刻</small><strong style="color:#b91c1c">第 ${escapeHtml(performance.rescheduleAtMin||'-')} 分</strong></div>`:''}<div class="metric"><small>算法运行时长</small><strong>${escapeHtml(runtimeText)}</strong></div><div class="metric"><small>最大连续人工</small><strong>${escapeHtml(maxHuman)} 分</strong></div><div class="metric"><small>预计完成</small><strong>${escapeHtml(o.finishTime)}</strong></div></div>
  <div class="table-wrap"><table><thead><tr><th>菜品</th><th>主要设备</th><th>开始</th><th>完成</th><th>耗时</th></tr></thead><tbody>${rows}</tbody></table></div>
  <div class="result-links"><a href="${dynamic?'/artifacts/05_dynamic_result.json':'/artifacts/02_static_result.json'}" target="_blank">接口响应 JSON</a><a href="${dynamic?'/artifacts/06_notifications.json':'/artifacts/03_static_notifications.json'}" target="_blank">通知 JSON</a><a href="${chart}" target="_blank">单独打开甘特图</a></div>
  <iframe src="${chart}?t=${Date.now()}" title="排程甘特图"></iframe><details><summary>查看原始接口响应</summary><pre>${escapeHtml(JSON.stringify(data,null,2))}</pre></details>`;
}
async function newPlan(){
  try{
    const response=await fetch('/new-plan',{method:'POST'});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'开始新一轮失败');
    state.active.clear();setMode(false);
    showMessage('新一轮已开始，请选择至少3道菜生成第一次静态排程。');
    el('result').innerHTML='<div class="empty">等待新一轮排程。</div>';
  }catch(error){showMessage(error.message,true);}
}
el('search').addEventListener('input',renderRecipes);el('manual-add').addEventListener('click',addManual);el('submit').addEventListener('click',submitSchedule);el('new-plan').addEventListener('click',newPlan);
(async()=>{try{await loadRecipes();await restoreSession();}catch(error){showMessage(error.message,true);}})();
</script></body></html>"""
