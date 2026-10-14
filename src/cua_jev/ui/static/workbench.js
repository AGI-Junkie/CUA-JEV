'use strict';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const goals = ['找到「项目简报」并打开', '将简报标记为已完成', '独立验证简报状态'];
let state = 'idle', step = 0, generation = 0, timer, frame = 0, scene = 'list', events = [], actionPhase = 0;
const faults = {focus:['目标窗口失去焦点','派发已阻止。重新确认同一目标窗口后再观察。'],stale:['当前帧已过期','候选已废弃，不能在旧坐标上派发。请重新观察。'],timeout:['模拟 API 超时','没有获得合法 JevDecision；未派发动作。请重新观察或重新规划。'],noeffect:['未验证预期效果','模拟输入凭证已记录，但目标状态仍未改变。不能把点击或图像变化计为成功。']};
function log(message, evidence='模拟事件') {
  events.push({time:new Date().toLocaleTimeString('zh-CN',{hour12:false}),message,evidence});
  $('#announce').textContent=message;
  $('#mini-log').innerHTML=events.slice(-4).reverse().map(e=>`<div>${esc(e.time)} · ${esc(e.message)}</div>`).join('');
  $('#history-log').innerHTML=events.map((e,i)=>`<div><strong>#${i+1} ${esc(e.time)}</strong> <span> / ${esc(e.evidence)}</span><br>${esc(e.message)}</div>`).join('');
}
function invalidate(){generation++;clearTimeout(timer);}
function later(fn,ms=850){const token=generation;timer=setTimeout(()=>{if(token===generation&&state==='running')fn();},ms);}
function status(label){$('#status').textContent=label;$('#announce').textContent=label;}
function phases(n){actionPhase=n;['proposed','input','verified'].forEach((id,i)=>{$('#'+id).classList.toggle('yes',n>i);$('#'+id).textContent=(n>i?'● ':'○ ')+['拟执行','已输入','已验证'][i];});}
function plan(){ $('#plan').innerHTML=goals.map((g,i)=>`<li data-index="${i<step?'✓':i+1}" class="${i<step?'done':i===step?'current':''}">${esc(g)}</li>`).join('');}
function draw(){
 const b=$('#layout').value==='b';
 const opened=scene!=='list', completed=scene==='done';
 const x=b?570:270,y=b?350:272;
 $('#screen').innerHTML=`<rect width="1280" height="800" fill="#eef1f5"/><rect width="1280" height="60" fill="#263c51"/><circle cx="27" cy="30" r="6" fill="#d38f83"/><circle cx="48" cy="30" r="6" fill="#e3c58e"/><circle cx="69" cy="30" r="6" fill="#83b6a5"/><text x="106" y="37" fill="white" font-family="sans-serif" font-size="18">LOCAL DEMO / 受控测试窗口 · ${b?'B':'A'}</text><rect x="0" y="60" width="${b?0:220}" height="740" fill="#fff"/>${b?'':`<text x="34" y="124" fill="#526378" font-size="20">WORKSPACE</text><rect x="22" y="161" width="176" height="46" rx="8" fill="#e4efec"/><text x="40" y="191" fill="#386754" font-size="18">我的文档</text>`}<text x="${b?80:270}" y="${b?140:130}" font-size="36" fill="#344b5e" font-family="sans-serif">${opened?'项目简报':'文档工作区'}</text><text x="${b?80:270}" y="${b?185:170}" font-size="18" fill="#8b969e">${opened?'任务状态由独立后置条件复核':'无账号 · 无费用 · 仅本地合成界面'}</text><rect x="${x}" y="${y}" width="${b?570:920}" height="${b?260:180}" rx="14" fill="white" stroke="#d3dfe0"/><text x="${x+30}" y="${y+55}" font-size="25" fill="#344b5e">${opened?'项目简报 / Q4':'项目简报'}</text><text x="${x+30}" y="${y+100}" font-size="18" fill="#7f8c94">${completed?'状态：已完成 ✓':opened?'状态：待处理':'本地草稿 · 等待处理'}</text><rect x="${x+30}" y="${y+120}" width="${opened?180:130}" height="42" rx="7" fill="${completed?'#50856d':'#2d6070'}"/><text x="${x+47}" y="${y+148}" font-size="17" fill="white">${completed?'已完成 ✓':opened?'标记为已完成':'打开简报 →'}</text>${b&&!opened?'<rect x="80" y="350" width="450" height="260" rx="14" fill="#e0e7ed"/><text x="110" y="405" font-size="23" fill="#687d90">团队资料</text>':''}<text x="${b?80:270}" y="720" fill="#95a0a4" font-size="15">合成画面 · 不包含真实桌面、网页或模型输出</text>`;
}
function observe(){frame++;$('#frame').textContent=`合成帧 #${frame} · 刚刚观察 · ${$('#layout').value.toUpperCase()}`;$('#focus').textContent='模拟 window=local-demo · focused · DPI 100%';$('#freshness').textContent='新鲜（模拟）';draw();}
function updateButtons(){$('#pause').disabled=!['running','paused'].includes(state);$('#pause').textContent=state==='paused'?'▶ 恢复模拟':'Ⅱ 暂停';$('#takeover').disabled=!['running','paused','failed'].includes(state);$('#start').disabled=state==='running';}
function fail(kind){invalidate();state='failed';$('#recovery').hidden=false;$('#failure-title').textContent=faults[kind][0];$('#failure-detail').textContent=faults[kind][1];status('需要恢复');if(kind==='stale')$('#freshness').textContent='过期 · 已阻止';if(kind==='focus'){$('#focus').textContent='模拟焦点丢失 · 已阻止';$('#scope').checked=false;}$('#verification').textContent='未通过';log(faults[kind][0]+'；循环已暂停','模拟异常 / 未通过');updateButtons();}
function cycle(){
 if(step>=goals.length){state='complete';status('已完成 · 模拟验证通过');$('#verification').textContent='3 / 3 预期效果通过';$('#stage-hint').textContent='这是回放验收结果；真实 API 与 Windows 尚未验证。';log('所有预期后置条件通过；模拟任务完成','模拟独立验证');updateButtons();return;}
 observe();plan();phases(1);$('#action').textContent=['click · 打开项目简报','click · 标记为已完成','observe · 复核状态'][step];$('#candidate').textContent=`fresh candidate / frame=${frame} / semantic=${['brief.open','brief.complete','brief.verify'][step]} / layout=${$('#layout').value}`;$('#proof').textContent='模拟 Jev 从当前合法候选中选择；真实模型调用数仍为 0。';log('新帧生成候选；模拟 Jev 选中 '+['brief.open','brief.complete','brief.verify'][step]);
 later(()=>{const fault=$('#fault').value;if(fault){$('#fault').value='';if(fault==='noeffect'){phases(2);log('模拟输入已记录，但状态未改变','模拟执行凭证');}fail(fault);return;}phases(2);$('#proof').textContent=`模拟执行凭证 receipt-${frame}；尚待独立验证。`;log(`模拟 receipt-${frame} 已记录`,'模拟执行凭证');later(()=>{scene=step===0?'detail':'done';draw();phases(3);$('#proof').textContent=['独立后置条件：document=brief 且详情可见。','独立后置条件：document=brief 且 status=completed。','独立复核：目标文档身份与完成状态同时匹配。'][step];$('#verification').textContent=`${step+1} / 3 预期效果通过`;log($('#proof').textContent,'模拟独立验证');step++;plan();later(cycle,1100);});});
}
function start(){if(!$('#scope').checked){status('请先确认目标窗口范围');$('#scope').focus();return;}if(!$('#task').value.trim()){status('请填写任务目标');$('#task').focus();return;}invalidate();step=0;scene='list';state='running';$('#recovery').hidden=true;$('#correction-panel').hidden=true;status('运行中 · 仅模拟');log('新建模拟 PlanContract；任务：'+$('#task').value.trim());updateButtons();cycle();}
$('#start').onclick=start;
$('#pause').onclick=()=>{if(state==='running'){invalidate();state='paused';status('已暂停 · 模拟回放');log('暂停：待处理回调已取消；不再推进模拟输入');}else{state='running';status('运行中 · 仅模拟');log('恢复：先重新观察，再生成候选');cycle();}updateButtons();};
$('#stop').onclick=()=>{invalidate();state='stopped';status('已急停 · 模拟回放');$('#recovery').hidden=true;$('#correction-panel').hidden=true;$('#scope').checked=false;$('#stage-hint').textContent='模拟回调已取消。真实桌面急停须等后端连接并确认。';log('急停：模拟调度已终止，范围确认已失效');updateButtons();};
$('#takeover').onclick=()=>{invalidate();state='takeover';$('#correction-panel').hidden=false;status('由你接管 · 模拟暂停');log('进入接管；模拟调度已取消');$('#correction').focus();updateButtons();};
$('#correct').onclick=()=>{if(!$('#correction').value.trim()){status('请填写接管纠正说明');return;}log('接管纠正：'+$('#correction').value.trim());$('#correction').value='';$('#scope').checked=false;$('#correction-panel').hidden=true;state='idle';status('纠正已记录 · 请重新确认范围');updateButtons();};
$('#reobserve').onclick=()=>{if(!$('#scope').checked){status('恢复前请重新确认目标窗口范围');$('#scope').focus();return;}invalidate();state='running';$('#recovery').hidden=true;status('重新观察 · 模拟');log('恢复：废弃旧候选，从新帧重试当前子目标');updateButtons();cycle();};
$('#replan').onclick=()=>{if(!$('#scope').checked){status('重新规划前请确认目标窗口范围');$('#scope').focus();return;}invalidate();step=0;scene='list';state='running';$('#recovery').hidden=true;status('重新规划 · 模拟');log('模拟重新规划：生成新的语义子目标计划');updateButtons();cycle();};
function scopeChanged(){invalidate();state='idle';step=0;scene='list';$('#scope').checked=false;$('#recovery').hidden=true;$('#correction-panel').hidden=true;$('#scope-text').textContent=`Local Demo · layout=${$('#layout').value.toUpperCase()} · document=brief · 1280 × 800 · DPI 100%`;status('范围或任务已变更 · 需要确认');phases(0);plan();draw();updateButtons();}
$('#scope').onchange=()=>{if(!$('#scope').checked&&['running','paused','failed'].includes(state)){invalidate();state='idle';$('#recovery').hidden=true;$('#correction-panel').hidden=true;status('范围确认已撤回 · 模拟调度已取消');log('撤回范围确认：待处理回调已取消');updateButtons();}};
$('#layout').onchange=scopeChanged;$('#task').oninput=scopeChanged;$('#context').oninput=scopeChanged;
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>{document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==button.dataset.view);document.querySelectorAll('[data-view]').forEach(b=>b.removeAttribute('aria-current'));button.setAttribute('aria-current','page');});
$('#settings-form').onsubmit=e=>{e.preventDefault();$('#planner-key').value='';$('#jev-key').value='';$('#settings-status').textContent='模拟配置已确认；密钥已清空。未保存、未发送、未测试真实模型连接。';log('会话内模拟配置已确认；API key 清空，未调用模型。','模拟配置');};
$('#load-history').onclick=async()=>{try{const res=await fetch('/api/runs');if(!res.ok)throw Error(`HTTP ${res.status}`);const value=await res.json();const rows=Array.isArray(value)?value:(value.runs||[]);$('#backend-history').textContent=rows.length?rows.slice(0,20).map(r=>`${r.id} / ${r.status||'未知状态'} / ${r.task||'未知任务'}（原有后端记录）`).join('\n'):'现有后端没有运行记录。';}catch(e){$('#backend-history').textContent='未能读取后端历史：'+e.message;}};
plan();draw();log('工作台已打开；所有画面、候选、决策与证据均为模拟。');
