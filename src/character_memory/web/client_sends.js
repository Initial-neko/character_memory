/* Client-owned send intent. Server facts/receipts remain authoritative. */
(() => {
  const CM = window.CM, records = new Map(), flights = new Map();
  let opening, revision = 0;
  const handles = (path, options={}) => String(options.method||'GET').toUpperCase()==='POST' &&
    typeof options.body==='string' && (/^\/v1\/chat\/messages$/.test(path)||/^\/v1\/groups\/[^/]+\/messages$/.test(path));
  function open() {
    if (!opening) opening = new Promise((resolve,reject)=>{
      const request=indexedDB.open('character-memory-client-sends',2);
      request.onupgradeneeded=()=>{
        const db=request.result, store=db.objectStoreNames.contains('sends')
          ? request.transaction.objectStore('sends') : db.createObjectStore('sends',{keyPath:'id'});
        if(!store.indexNames.contains('state'))store.createIndex('state','state');
      };
      request.onsuccess=()=>{const db=request.result;db.onversionchange=()=>{db.close();opening=null};resolve(db)};
      request.onerror=()=>reject(request.error);
      request.onblocked=()=>reject(new Error('发送记录被其他页面占用'));
    }).catch(error=>{opening=null;throw error});
    return opening;
  }
  async function transact(mode, action) {
    const db=await open();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('sends',mode), store=tx.objectStore('sends');let value;
      tx.oncomplete=()=>resolve(value);tx.onabort=()=>reject(tx.error||new Error('发送记录未保存'));tx.onerror=()=>{};
      action(store,result=>{value=result});
    });
  }
  const storage={
    list:()=>transact('readonly',(s,done)=>{s.getAll().onsuccess=e=>done(e.target.result)}),
    get:id=>transact('readonly',(s,done)=>{s.get(id).onsuccess=e=>done(e.target.result)}),
    put:record=>transact('readwrite',(s,done)=>{
      s.get(record.id).onsuccess=e=>{
        const existing=e.target.result;
        // A late transport failure in another tab must not undo an acknowledged send.
        if(existing?.state==='accepted' && record.state!=='accepted'){done(existing);return}
        s.put(record);
        if(record.state==='accepted')s.index('state').getAll('accepted').onsuccess=result=>{
          const accepted=result.target.result.sort((a,b)=>(b.ackAt||b.createdAt)-(a.ackAt||a.createdAt));
          for(const old of accepted.slice(100))s.delete(old.id);
        };
        done(record);
      };
    }),
  };
  function scope(path,payload){return JSON.stringify(path==='/v1/chat/messages'
    ? ['DIRECT',payload.character_id,payload.conversation_id]
    : ['GROUP',decodeURIComponent(path.split('/')[3])]);}
  function valid(record){
    if(!record||record.schema!==1||record.origin!==location.origin||typeof record.id!=='string'||!handles(record.path,{method:'POST',body:record.body}))return false;
    try {const body=JSON.parse(record.body);return body.request_id===record.id&&scope(record.path,body)===record.scope}
    catch{return false}
  }
  function currentScope(){
    if(!CM.state.characterId)return null;
    return JSON.stringify(CM.isGroupConversation()?['GROUP',CM.state.conversation.groupId]
      :['DIRECT',CM.state.characterId,CM.conversationIdFor(CM.state.characterId)]);
  }
  async function save(record){
    const saved=await storage.put(record);records.set(saved.id,saved);
    const accepted=[...records.values()].filter(r=>r.state==='accepted').sort((a,b)=>(b.ackAt||b.createdAt)-(a.ackAt||a.createdAt));
    for(const old of accepted.slice(100))records.delete(old.id);
    revision++;render();return saved;
  }
  async function refresh(){
    const version=revision, list=await storage.list();
    if(version!==revision)return;
    records.clear();for(const record of list)if(valid(record))records.set(record.id,record);
    render();
  }
  function fail(record,message){const error=new Error(message);error.clientSendId=record.id;return error;}
  async function dispatch(record,{fresh=false}={}){
    if(flights.has(record.id))return flights.get(record.id);
    const operation=(async()=>{
      const current=await storage.get(record.id);
      if(!valid(current))throw fail(record,'原发送记录不可用，未发出请求');
      if(current.state==='accepted'){records.set(current.id,current);render();return current.ack;}
      if(current.state==='rejected')throw fail(current,'请编辑为新消息，不自动重放被拒绝的发送');
      record={...current,state:'retrying'};
      try {record=await save(record)}catch{throw fail(record,'无法保存发送记录，本次未发送');}
      if(record.state==='accepted')return record.ack;
      try {
        const response=await fetch(record.path,{method:'POST',headers:{'Content-Type':'application/json'},body:record.body});
        if(!response.ok){
          const detail=await response.text();
          record={...record,state:fresh&&!record.uncertain&&response.status>=400&&response.status<500?'rejected':'unknown',uncertain:!fresh||Boolean(record.uncertain)||response.status>=500,error:`${response.status} ${detail}`};
          await save(record);throw fail(record,record.error);
        }
        const result=await response.json();
        if(response.status!==202||result.accepted!==true||result.request_receipt?.request_id!==record.id||!Number.isSafeInteger(result.event_id))
          throw fail(record,'没有收到可核实的发送确认');
        const body=JSON.parse(record.body);
        // Keep only a small accepted projection. Large pending image bytes are no longer needed.
        delete body.image;
        record={...record,state:'accepted',ackAt:Date.now(),body:JSON.stringify(body),ack:{event_id:result.event_id,message:result.message},error:null};
        await save(record);
        return result;
      } catch(error){
        if(record.state!=='accepted'&&record.state!=='rejected'){
          record={...record,state:'unknown',uncertain:true};
          try{await save(record)}catch{records.set(record.id,record);revision++;render()}
        }
        if(error.clientSendId)throw error;
        throw fail(record,'发送结果待确认，请重试原发送');
      }
    })();
    flights.set(record.id,operation);render();
    try{return await operation}finally{flights.delete(record.id);render()}
  }
  async function send(path,options){
    let payload=JSON.parse(options.body);
    if(!payload||typeof payload!=='object'||Array.isArray(payload))throw Error('消息格式不正确');
    const selected=scope(path,payload);
    const id=crypto.randomUUID();payload={...payload,request_id:id};
    const record={schema:1,origin:location.origin,id,path,scope:selected,body:JSON.stringify(payload),
      state:'retrying',uncertain:false,createdAt:Date.now(),preview:String(payload.message||''),
      attachment:payload.image?.filename||payload.sticker_id||'',ack:null};
    try{await save(record)}catch{
      if(currentScope()===selected&&!CM.dom.input.value)CM.dom.input.value=String(payload.message||'');
      throw Error('无法保存发送记录，本次未发送；请检查浏览器存储空间');
    }
    return dispatch(record,{fresh:true});
  }
  async function retry(id){
    const record=records.get(id)||await storage.get(id);
    if(!valid(record)||record.scope!==currentScope())throw Error('请在原会话中重试');
    const result=await dispatch(record);
    if(record.scope===currentScope()){
      if(!CM.isGroupConversation()&&result.message)CM.mergeDirectMessage(result.message);
      else await CM.features.groups?.reconcileLatest?.(CM.state.conversation.groupId);
    }
    return result;
  }
  async function edit(record){
    const payload=JSON.parse(record.body);
    if(payload.life_reference){
      const url=new URL(location.href);url.searchParams.set('life_character',payload.life_reference.character_id);url.searchParams.set('life_key',payload.life_reference.key);
      history.replaceState(null,'',url);await CM.openLifeReference();
      if(CM.state.lifeReference?.key!==payload.life_reference.key)return;
    }
    CM.dom.input.value=record.preview;
    if(payload.image){
      CM.features.images?.openDataDraft({...payload.image,source:'FILE_PICKER'});
      const caption=document.getElementById('imageCaption');if(caption)caption.value=record.preview;
    }else if(payload.sticker_id){CM.features.stickers?.open?.();}
    else CM.dom.input.focus();
  }
  const labels={unknown:['发送结果待确认','还没收到确认。重试会继续这一次发送，不会另建一条。'],
    retrying:['正在确认原发送…','正在沿用原来的发送记录，请稍候。'],
    accepted:['已送达','消息已保存，接下来等待伙伴回应。'],
    rejected:['未能发送','服务已明确拒绝这次发送。内容保留，编辑后将作为一条新消息发送。']};
  function render(){
    const chat=CM.dom?.chat;if(!chat||!currentScope())return;
    const candidates=[...records.values()].filter(r=>r.scope===currentScope()).sort((a,b)=>a.createdAt-b.createdAt);
    const rows=[...chat.querySelectorAll('.message-row.user[data-message-id]')];
    const host=r=>rows.find(n=>n.dataset.clientRequestId===r.id);
    const visible=candidates.filter(r=>r.state!=='accepted'||host(r));
    const state=r=>r.state==='retrying'&&!flights.has(r.id)?'unknown':r.state;
    const signature=JSON.stringify(visible.map(r=>[r.id,state(r),Boolean(host(r))]));
    if(chat.dataset.clientSendVersion===signature&&chat.querySelectorAll('.client-send-record').length===visible.length)return;
    const followBottom=window.scrollY+window.innerHeight>=document.body.scrollHeight-80;
    chat.querySelectorAll('.client-send-record,[data-client-pending]').forEach(n=>n.remove());
    for(const record of visible){
      const status=state(record),[title,text]=labels[status]||labels.unknown;
      let parent=host(record)?.querySelector('.bubble-wrap');
      if(!parent){
        const row=document.createElement('article');row.className='message-row user client-pending';row.dataset.clientPending=record.id;
        const avatar=document.createElement('div');avatar.className='avatar';parent=document.createElement('div');parent.className='bubble-wrap';
        const bubble=document.createElement('div');bubble.className='bubble';bubble.textContent=record.preview||'消息';
        if(record.attachment){const attachment=document.createElement('div');attachment.className='client-send-attachment';attachment.textContent=`附件 · ${record.attachment}`;bubble.append(attachment)}
        parent.append(bubble);row.append(avatar,parent);chat.append(row);
      }
      const card=document.createElement('section');card.className='client-send-record';card.dataset.state=status;card.dataset.sendId=record.id;card.setAttribute('role','status');card.setAttribute('aria-live','polite');
      const strong=document.createElement('strong');strong.textContent=title;const description=document.createElement('p');description.textContent=text;card.append(strong,description);
      if(status!=='accepted'){
        const actions=document.createElement('div');actions.className='client-send-actions';
        const primary=document.createElement('button');primary.type='button';primary.className='client-send-primary';primary.disabled=status==='retrying';
        primary.textContent=status==='rejected'?'编辑为新消息':status==='retrying'?'正在确认…':'重试原发送';
        primary.title=status==='rejected'?'把保留内容放入输入框，下次发送创建新身份':'使用原来的内容与请求身份，不创建第二条发送';
        primary.onclick=()=>{if(status==='rejected')edit(record).catch(()=>{});else retry(record.id).catch(()=>{})};
        const secondary=document.createElement('button');secondary.type='button';secondary.textContent=status==='rejected'?'保留草稿':'另写一条';secondary.title='原记录继续保留；输入框中的新消息使用独立发送身份';
        secondary.onclick=()=>CM.dom.input.focus();actions.append(primary,secondary);card.append(actions);
      }
      parent.append(card);
    }
    chat.dataset.clientSendVersion=signature;
    if(followBottom)CM.scrollToBottom(false);
  }
  CM.clientSends={handles,send,retry,storage,list:()=>storage.list(),render};
  CM.on('ready',async()=>{
    await refresh().catch(()=>{});
    new MutationObserver(()=>render()).observe(CM.dom.chat,{childList:true,subtree:true});
  });
  CM.on('conversationChanged',render);CM.on('historyLoaded',render);
  window.addEventListener('focus',()=>refresh().catch(()=>{}));
})();
