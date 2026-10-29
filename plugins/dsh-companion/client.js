window.__ModuleLoader__.load({id:'@local/dsh-companion',factory(require){const React=require('react');return {inject:['slots'],apply(ctx){let disposed=false,components;ctx.effect(()=>()=>{disposed=true;components?.dispose()});import('/companion/assets/client-ui.mjs').then(module=>{if(disposed)return;components=module.createComponents(React);const {Trigger,Overlay,Settings}=components;
ctx.slots.inject('conversation.input.left',()=>ctx.slots.register({name:'conversation.input.left',id:'companion-capsule',order:18,label:'陪伴'},Trigger));
ctx.slots.inject('conversation.input.overlay',()=>ctx.slots.register({name:'conversation.input.overlay',id:'companion-capsule',order:18,label:'陪伴'},Overlay));
ctx.slots.inject('settings.section',()=>ctx.slots.register({name:'settings.section',id:'companion-capsule',order:35,label:'陪伴'},Settings));
}).catch(error=>console.error('Companion UI unavailable:',error.message))}}}});
