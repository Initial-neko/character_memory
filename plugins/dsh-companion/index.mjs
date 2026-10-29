import z from '@deepseek-ai/schemastery';
import {ConfigStore,DEFAULTS} from './src/config.mjs';
import {createHandler,tapReply} from './src/http.mjs';
export const inject=['webServer','settings'];
export const Config=z.object({options:z.string().default(JSON.stringify({revision:0,value:DEFAULTS})).description('陪伴设置的版本化快照；通过陪伴设置面板修改，无密钥。保存不启动设备或模型。').volatile()});
export function apply(ctx,config){
  let raw=config?.options;for(let i=0;i<8&&typeof raw?.get==='function';i++)raw=raw.get();
  const store=new ConfigStore({initial:raw?JSON.parse(raw):undefined,persist:async next=>ctx.settings.update('companion-capsule',{options:JSON.stringify(next)})});
  const handler=createHandler({store});
  ctx.effect(()=>{const release=ctx.webServer.register({kind:'prefix',path:'/companion',handler});return()=>{handler.dispose();if(typeof release==='function')release()}});
  ctx.on('llm/stream',(options,next)=>{
    if(!options.sessionId||options.purpose!==undefined||!handler.hasListeners(options.sessionId))return next();
    return tapReply(next(),text=>handler.emitReply(options.sessionId,text));
  });
}
