/**
 * The host slot props expose no attachment API, but the composer keeps a hidden
 * file input that runs the same pipeline as a user pick. Fill it and wait until
 * the thumbnails appear, so submit never races the upload.
 */
export async function attachThroughInput(files, {
  document = globalThis.document,
  dataTransfer = typeof DataTransfer === 'function' ? new DataTransfer() : null,
  timeoutMs = 4000,
} = {}) {
  const input = document?.querySelector?.('input[type=file]');
  if (!input) throw Error('native image attachment interface unavailable');
  if (!dataTransfer?.items?.add) throw Error('native attachment transport unavailable');
  const count = () => document.querySelectorAll?.('img[src^="blob:"]').length ?? 0;
  const before = count();
  for (const file of files) dataTransfer.items.add(file);
  input.files = dataTransfer.files;
  input.dispatchEvent(new Event('change', {bubbles: true}));
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (count() >= before + files.length) return true;
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  throw Error('native attachment did not appear in the composer');
}

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
    p.inputActions.setDraft(text);
    if(files.length){
      const input=globalThis.document?.querySelector?.('input[type=file]');
      if(input)await attachThroughInput(files);
      else if(typeof p.addFiles==='function'){const error=p.addFiles(files);if(error)throw Error(String(error))}
      else throw Error('native image attachment interface unavailable');
    }
    await p.inputActions.submit();this.current();
  }
}
