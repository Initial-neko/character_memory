(() => {
  const CM=window.CM;
  if(!CM) return;
  let data={encounters:[],active_character_count:0,active_character_soft_limit:10,active_character_limit:20};
  let refreshing=null;
  let activeView='today',refreshView=null,requestEpoch=0;
  const path=id=>`/v1/encounters/${encodeURIComponent(id)}`;
  async function refresh(view=activeView,{append=false}={}){
    if(refreshing&&refreshView===view&&!append) return refreshing;
    activeView=view;refreshView=view;const epoch=++requestEpoch;
    const offset=append?data.next_offset||0:0;
    refreshing=CM.api(`/v1/encounters?limit=50&include_closed=true&view=${view}&offset=${offset}`).then(result=>{
      if(epoch!==requestEpoch)return result;
      data={...result,encounters:append?[...data.encounters,...result.encounters]:result.encounters};
      CM.emit('encountersChanged',data).catch(console.error);return data;
    }).finally(()=>{if(epoch===requestEpoch)refreshing=null;});
    return refreshing;
  }
  async function detail(id){return (await CM.api(path(id))).candidate;}
  async function post(id,suffix,body){return CM.api(path(id)+suffix,{method:'POST',...(body?{body:JSON.stringify(body)}:{})});}
  async function accept(id){
    const current=Number(data.active_character_count||0),soft=Number(data.active_character_soft_limit||10),hard=Number(data.active_character_limit||20);
    if(current>=hard)throw Error(`正式角色已达到 ${hard} 位上限，请先归档一位人物。`);
    if(!window.confirm(`让这场相遇继续下去？TA 将加入正式人物，对话会保留。${current>=soft?`当前已有 ${current} 位角色，将超过 ${soft} 位提醒阈值。`:''}`))return null;
    const result=await post(id,`/accept${current>=soft?'?confirm_over_soft_limit=true':''}`);
    await CM.loadCharacters();await refresh();return result.candidate;
  }
  CM.registerFeature('encounter',{
    refresh,detail,get state(){return data;},accept,
    seen:id=>post(id,'/seen'),send:(id,message)=>post(id,'/messages',{message}),
    dismiss:async id=>{const result=await post(id,'/dismiss');await refresh();return result;},
    portrait:id=>post(id,'/portrait'),retry:id=>post(id,'/reply/retry'),requestCall:id=>post(id,'/calls'),
    history:(id,before)=>CM.api(`${path(id)}/messages?limit=50${before?`&before_id=${before}`:''}`),
  });
})();
