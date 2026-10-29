/** Adapts inspected DSH composer slot props; never owns another conversation. */
export class HostBridge {
  #read;#fingerprint='';#revision=0;
  constructor(read){this.#read=read}
  current(){
    const p=this.#read();const sessionId=p.sessionId,draft=p.input?.draft??'',attachments=p.input?.attachmentIds??[];
    const fingerprint=JSON.stringify([sessionId,draft,attachments]);if(fingerprint!==this.#fingerprint){this.#fingerprint=fingerprint;this.#revision++}
    return {sessionId,draft,draftRevision:this.#revision};
  }
  #matches(token){const current=this.current();return !!current.sessionId&&token?.sessionId===current.sessionId&&token?.draftRevision===current.draftRevision}
  applyTranscript(token,text){
    if(!this.#matches(token)||typeof text!=='string'||!text.trim())return false;
    const actions=this.#read().inputActions;if(typeof actions?.setDraft!=='function')return false;
    const draft=this.current().draft;actions.setDraft(draft?draft+'\n'+text.trim():text.trim());this.current();return true;
  }
  async submit({token,text,files=[]}){
    if(!this.#matches(token))throw Error('native input or session changed; review before sending');
    const p=this.#read();if(typeof p.inputActions?.submit!=='function'||typeof p.inputActions?.setDraft!=='function')throw Error('native submit interface unavailable');
    if(files.length){if(typeof p.addFiles!=='function')throw Error('native image attachment interface unavailable');const error=p.addFiles(files);if(error)throw Error(String(error))}
    p.inputActions.setDraft(text);await p.inputActions.submit();this.current();
  }
}
