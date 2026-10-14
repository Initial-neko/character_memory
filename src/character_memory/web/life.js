(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const sourceNames={events:"人物事件",group:"共享群聊事件",post:"Space 动态",comment:"Space 评论",world:"World 决策记录",intent:"未来意图",space_run:"Space 机会记录",intent_audit:"意图处理记录"};
  const channels = {DIRECT:"私聊",GROUP:"群聊",SPACE:"Space",WORLD:"World",INTENT:"Intent"};
  const actions={MESSAGE:"发送消息",PROACTIVE_MESSAGE:"主动表达",SPACE_COMMENT:"发表了评论",SPACE_LIKE:"点赞",NO_REPLY:"未回复",VOICE:"语音表达",STICKER:"发送表情",IMAGE:"发送图片"};
  const statuses = {CALL_INVITATION_PROPOSED:"仅提出邀请，未证明通话",NO_POST:"未发动态",POSTED:"已发动态",RECORDED:"已记录",SUCCESS:"已完成",SUCCEEDED:"已完成",APPLIED:"已处理",FAILED:"失败",DENIED:"被拒绝",TIMEOUT:"超时",NO_ACTION:"未行动",SKIPPED:"未执行",PLANNING:"决策中",PENDING:"计划中",PROCESSING:"处理中",EXECUTED:"已执行",ABANDONED:"已放弃",EXPIRED:"已失效",DEFERRED:"已延期",COMPLETED:"已完成"};
  let profiles=[], items=[], selected="", view="life", channel="ALL", cursor=null, request=0, undated=0;
  const dateString = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  const formatted = t => {if(!t)return "未设置";const d=new Date(t);return Number.isNaN(+d)?"时间未记录":d.toLocaleString("zh-CN",{month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"});};
  const safeURL = value => {try {const u=new URL(value,location.origin);return ["http:","https:"].includes(u.protocol)?u.href:"";}catch{return "";}};
  async function get(url) {const r=await fetch(url);if(!r.ok){const data=await r.json().catch(()=>({}));throw new Error(data.detail||`请求失败 (${r.status})`);}return r.json();}
  function render() {
    const peer=$("peer").value, visible=peer?items.filter(i=>i.participants.some(p=>p.id===peer)):items;
    $("timeline").innerHTML=visible.map(i=>`<div class="timeline-row ${selected===i.key?"selected":""}"><time datetime="${esc(i.time)}">${esc(new Date(i.time).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"}))}</time><button type="button" class="event-card" data-key="${esc(i.key)}" aria-pressed="${selected===i.key}"><div class="card-head"><h2>${esc(i.title)}</h2><span class="badge ${["FAILED","DENIED","NO_ACTION","TIMEOUT"].includes(i.status)?"warn":i.kind==="intent"?"plan":""}">${esc(channels[i.channel]||i.channel)} · ${esc(i.intent?.time_status==="INVALID"?"时间无效，到期状态未知":i.intent?.past_expiry?"有效期已过，待处理":i.intent?.due?"已到期，待执行":statuses[i.status]||i.status)}</span></div><p>${esc(i.content)}</p>${i.reading?`<small>阅读范围：${esc(i.reading.scope||"未记录")} · ${i.state_change?"存在状态更新":"未关联状态更新"}</small>`:""}<div class="chips">${i.state_change?"<span>心理状态更新</span>":""}${i.memories.length?`<span>关联记忆 ${i.memories.length} 条</span>`:""}</div><small>来源：${esc(sourceNames[i.source.kind]||i.source.kind)} · ${esc(i.source.id)}</small></button></div>`).join("");
    $("status").textContent=visible.length?`${visible.length} 条真实记录`:"这个范围内没有已记录的经历，不代表人物主动选择了沉默。";
    if(undated)$("status").textContent+=`另有 ${undated} 条符合筛选的记录时间无效，无法归入日期。`;
    $("more").hidden=!cursor;
    const current=visible.find(i=>i.key===selected);
    if(current){detail(current);executionButton(current);}else $("details").innerHTML='<p class="empty-detail">选择一条经历，查看来源、结果与变化。</p>';
  }
  function detail(i) {
    const state=i.state_change, reading=i.reading;
    $("details").innerHTML=`<h2>这次经历</h2><p class="detail-subtitle">${esc(channels[i.channel])} · ${esc(formatted(i.time))}</p><dl class="facts"><dt>来源</dt><dd>${esc(sourceNames[i.source.kind]||i.source.kind)} · ${esc(i.source.id)}</dd><dt>触发</dt><dd>${esc(i.title)}</dd><dt>结果</dt><dd>${esc((i.decision.result||"").split("、").map(a=>actions[a]||statuses[a]||a).join("、")||statuses[i.status]||i.status)}</dd></dl><div class="source-links"><a href="#life-original">原始事件 ↗</a></div><h3 id="life-original">原始记录</h3><p>${esc(i.content||"未记录正文")}</p><div class="source-links">${reading?.url&&safeURL(reading.url)?`<a href="${esc(safeURL(reading.url))}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>`:""}${i.source.post_id?`<span class="hint">关联动态 #${esc(i.source.post_id)}</span>`:""}${i.source.conversation_id?`<span class="hint">群聊 ${esc(i.source.conversation_id)}</span>`:""}</div><h3>已记录的决策</h3><p>${esc(i.decision.basis||"未记录明确依据，不推测隐藏思考过程。")}</p>${i.decision.error?`<p class="badge warn">${esc(i.decision.error)}</p>`:""}${reading?`<h3>实际阅读范围</h3><p>${esc(reading.scope||"未记录")} · ${reading.truncated===true?"已截断":reading.truncated===false?"未截断 Feed 文本":"截断情况未知"}<br>实际提供 ${esc(reading.read_characters??"未知")} 字符 / Feed 可用 ${esc(reading.available_characters??"未知")} 字符。未据此访问链接原文。</p><h3>人物评估摘要（非原文）</h3><p>${esc(reading.summary)}</p>`:""}<h3>实际发生的变化</h3>${state?`<div class="state-pair"><article>之前<p>${esc(state.before||"没有更早的状态记录")}</p></article><span>→</span><article>之后<p>${esc(state.after||"未记录")}</p></article></div><p class="hint">来自已保存的心理状态版本；事件关联不自动证明心理因果。</p>`:'<p class="hint">未关联心理状态更新，不补写变化。</p>'}<h3>关联记忆</h3>${i.memories.length?i.memories.map(m=>`<article class="memory-card" id="memory-${m.id}"><strong>记忆 #${m.id}</strong><p>${esc(m.content)}</p><small>${m.active?"有效":"已停用"}${m.superseded_by?` · 已由 #${m.superseded_by} 替代`:""} · 来源：${esc(sourceNames[i.source.kind])} #${esc(i.source.id)}</small></article>`).join(""):'<p class="hint">没有关联的已保存记忆。</p>'}<h3>与谁共同经历</h3>${i.participants.length?i.participants.map(p=>`<p>${esc(profiles.find(c=>c.id===p.id)?.name||p.name)} <button data-peer="${esc(p.id)}">查看共同经历 ↗</button></p>`).join(""):'<p class="hint">未记录可确认的互动对象。</p>'}${i.intent?`<h3>未来表达意图</h3><p>${i.intent.time_status==="INVALID"?"时间数据无效，到期状态未知。<br>":""}${i.intent.due?"已到期，尚未执行。<br>":""}${i.intent.past_expiry?"有效期已过，尚未终结处理。<br>":""}延期次数：${esc(i.intent.defer_count)}<br>最早：${esc(formatted(i.intent.earliest_at))}<br>失效：${esc(formatted(i.intent.expires_at))}</p>${i.intent.audit.map(a=>`<div class="audit-row">${esc(formatted(a.decided_at))} · ${esc(a.decision)}${a.new_earliest_at?`<br>调整至 ${esc(formatted(a.new_earliest_at))}`:""}</div>`).join("")}<p class="hint">未来计划不是已经发生的经历。</p>`:""}<div class="participate"><button type="button" data-life-chat ${["events","group","post","comment"].includes(i.kind)?"":"disabled"} title="在原有会话中准备来源草稿；确认发送后才调用模型">围绕这件事聊天</button><span class="hint" aria-live="polite" data-chat-status></span></div>`;
  }
  function executionButton(item) {
    const id=item.decision?.execution_request_id;
    if(!id)return;
    const button=document.createElement("button"), area=document.createElement("section");
    button.type="button";button.textContent="查看外部执行";
    button.title="只读取本次能力执行的任务、来源和用量记录，不重新调用模型。";
    area.setAttribute("aria-live","polite");
    $("details").append(button,area);
    button.onclick=async()=>{
      const character=$("character").value, version=request;
      button.disabled=true;area.textContent="正在读取执行记录…";
      try {
        const data=await get(`/v1/characters/${encodeURIComponent(character)}/executions/${encodeURIComponent(id)}`);
        if(character!==$("character").value||version!==request||selected!==item.key)return;
        const e=data.execution||{}, cost=e.estimated_cost_usd;
        const steps=(data.tool_steps||[]).map(step=>`<article class="audit-row"><strong>${esc(step.tool)} · ${esc(({SUCCESS:"成功返回",FAILED:"失败",DENIED:"未获许可",RUNNING:"未完成，结果未知"})[step.status]||step.status)}</strong><p>执行请求：${esc(step.request_id)}<br>本地步骤：${esc(step.step_id)}<br>开始：${esc(formatted(step.started_at))}<br>结束：${step.completed_at?esc(formatted(step.completed_at)):"未知"} · ${esc(step.elapsed_ms??"未知")} 毫秒</p><p>查询：${esc(step.arguments?.query||"未记录合法参数")}</p>${step.reason?`<p>${esc(step.reason)}</p>`:""}${(step.result?.observations||[]).map(row=>`<p>${esc(row.title)}${row.title_truncated?"（标题已截断）":""} · ${row.excerpt_truncated?"详情节选已截断":"详情节选未截断"}（工具提供 ${esc(row.available_characters??"未知")} 字符）<br>${esc(row.content)}</p>${row.url_truncated?"来源地址已截断，请查看上方完整来源凭据。":safeURL(row.url)?`<a href="${esc(safeURL(row.url))}" target="_blank" rel="noopener noreferrer">工具结果来源 ↗</a>`:""}`).join("")}</article>`).join("");
        const toolDetails=`<details><summary>受控工具步骤（${esc(data.tool_steps?.length??0)} 条）</summary><p class="hint">这里只展示真实工具回调，结果是外部材料，不是人物自己的经历；本地步骤编号不是 Claude 请求编号。${data.tool_steps_truncated?"步骤记录达到上限，额外回调未展示。":""}</p>${steps||`<p>${data.tool_steps_state==="RECORDED"?"没有受控工具回调，不能据此声称工具执行成功。":"旧执行未记录步骤，结果未知。"}</p>`}</details>`;
        area.innerHTML=`<h3>外部执行 · ${esc(data.backend)}</h3><p>结果：${esc(statuses[data.status]||data.status)}${data.reason?` · ${esc(data.reason)}`:""}</p><h3>本次发送的任务</h3><p>${esc(data.task)}</p><h3>助手可见回复（非原文）</h3><p>${esc(data.assistant_response||"回复详情未记录或已过保留期")}</p><h3>实际来源凭据</h3>${(data.source_receipts||[]).map(s=>`<p>${safeURL(s.url)?`<a href="${esc(safeURL(s.url))}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>`:"未记录有效来源链接"} · ${esc(s.content_scope||"读取范围未知")} · ${esc(s.read_characters??"未知")} 字符</p>`).join("")||"<p>没有可核查的来源，不据此声称阅读过。</p>"}<h3>实际工具范围</h3><p>能力：${esc(data.capability||"未知")} · 工具调用 ${esc(e.tool_calls??"未知")} 次<br>工具：${e.effective_tools==null?"未采样":esc(e.effective_tools.join("、")||"无工具")}<br>MCP：${e.mcp_servers==null?"未采样":esc(e.mcp_servers.map(s=>`${s.name}: ${s.status}`).join("、")||"无连接")}<br>CLI：${esc(e.cli_version||"未记录")}</p>${toolDetails}<h3>执行用量</h3><p>输入 Token：${esc(e.input_tokens??"未知")} · 输出 Token：${esc(e.output_tokens??"未知")}<br>估算费用：${cost==null?"未知":`$${esc(cost)}`} · ${esc(e.usage_state||"UNKNOWN")}<br>一次执行可能包含多次模型调用；估算不是订阅实际扣费或剩余额度。</p>`;
      } catch(error) {
        if(character===$("character").value&&version===request&&selected===item.key)area.textContent="执行详情读取失败，未重试执行。";
      } finally {button.disabled=false;}
    };
  }
  async function load(more=false) {
    if(!$("character").value)return;
    const token=++request;
    if(!more){items=[];selected="";$("timeline").replaceChildren();$("details").innerHTML='<p class="empty-detail">正在读取所选人物的经历…</p>';}
    document.body.classList.add("loading");$("status").textContent="正在读取真实记录…";$("retry").hidden=true;
    const qs=new URLSearchParams({day:$("day").value,offset:String(-new Date($("day").value+"T12:00:00").getTimezoneOffset()),view,channel,query:$("search").value,peer:view==="relationships"?$("peer").value:""});
    if(more&&cursor)qs.set("cursor",cursor);
    try {const data=await get(`/v1/characters/${encodeURIComponent($("character").value)}/life?${qs}`);if(token!==request)return;items=more?[...items,...data.items]:data.items;cursor=data.next_cursor;undated=data.undated_count||0;selected=items[0]?.key||"";
      const previous=$("peer").value, peers=new Map([{id:"user",name:"用户"},...profiles.filter(p=>p.id!==$("character").value),...items.flatMap(i=>i.participants)].map(p=>[p.id,p]));$("peer").innerHTML='<option value="">全部对象</option>'+[...peers.values()].map(p=>`<option value="${esc(p.id)}">${esc(profiles.find(c=>c.id===p.id)?.name||p.name)}</option>`).join("");$("peer").value=peers.has(previous)?previous:"";render();
    } catch(e) {if(token!==request)return;items=[];cursor=null;$("timeline").replaceChildren();$("details").innerHTML='<p class="hint">记录加载失败，未展示旧人物的数据。</p>';$("status").textContent=e.message;$("retry").hidden=false;$("more").hidden=true;}finally{if(token===request)document.body.classList.remove("loading");}
  }
  function identity() {const p=profiles.find(i=>i.id===$("character").value);$("heading").textContent=`人物生活 · ${p?.name||p?.id||""}`;$("avatar").textContent=(p?.name||"人").slice(0,1);if(p?.avatar_url&&safeURL(p.avatar_url)){const img=document.createElement("img");img.src=safeURL(p.avatar_url);img.alt="";img.onerror=()=>img.remove();$("avatar").append(img);}$("dateLabel").textContent=$("day").value;}
  document.querySelectorAll('[data-view]').forEach(b=>b.onclick=async()=>{view=b.dataset.view;$("peerLabel").hidden=view!=="relationships";$("peer").value="";document.querySelectorAll('[data-view]').forEach(x=>x.setAttribute("aria-selected",String(x===b)));await load();});
  document.querySelectorAll('[data-channel]').forEach(b=>b.onclick=()=>{channel=b.dataset.channel;document.querySelectorAll('[data-channel]').forEach(x=>x.setAttribute("aria-pressed",String(x===b)));load();});
  $("timeline").onclick=e=>{const b=e.target.closest('[data-key]');if(b){selected=b.dataset.key;render();}};
  $("details").onclick=async e=>{
    const chat=e.target.closest('[data-life-chat]');
    if(chat){
      const character=$("character").value, key=selected, token=request;
      chat.disabled=true;
      const status=$("details").querySelector('[data-chat-status]');status.textContent="正在核验来源…";
      try {
        await get(`/v1/characters/${encodeURIComponent(character)}/life/conversation?${new URLSearchParams({key})}`);
        if(character!==$("character").value||key!==selected||token!==request)return;
        location.assign(`/?${new URLSearchParams({life_character:character,life_key:key})}`);
      } catch(error){if(character===$("character").value&&key===selected&&token===request)status.textContent=error.message;}
      finally {chat.disabled=false;}
      return;
    }
    const b=e.target.closest('[data-peer]');if(b){view="relationships";$("peerLabel").hidden=false;document.querySelectorAll('[data-view]').forEach(x=>x.setAttribute("aria-selected",String(x.dataset.view===view)));$("peer").value=b.dataset.peer;await load();}};
  $("peer").onchange=()=>load();$("searchForm").onsubmit=e=>{e.preventDefault();load();};$("retry").onclick=()=>load();$("more").onclick=()=>load(true);$("character").onchange=()=>{identity();load();};$("day").onchange=()=>{identity();load();};$("today").onclick=()=>{$("day").value=dateString(new Date());identity();load();};
  for(const [id,delta] of [["previous",-1],["next",1]])$(id).onclick=()=>{const d=new Date($("day").value+"T12:00:00");d.setDate(d.getDate()+delta);$("day").value=dateString(d);identity();load();};
  $("day").value=dateString(new Date());
  get('/v1/characters?include_deferred=true').then(data=>{profiles=data.characters;$("character").innerHTML=profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name||p.id)}</option>`).join("");const wanted=new URLSearchParams(location.search).get("character");if(profiles.some(p=>p.id===wanted))$("character").value=wanted;identity();const desired=new URLSearchParams(location.search).get("view");if(profiles.length&&["changes","relationships","intents"].includes(desired))document.querySelector(`[data-view="${desired}"]`).click();else if(profiles.length)load();else $("status").textContent="还没有可查看的人物。";}).catch(e=>{$("status").textContent=e.message;});
})();
