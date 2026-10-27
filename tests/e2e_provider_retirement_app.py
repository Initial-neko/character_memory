"""Actual API, shared real model adapter, local MockTransport, no provider network."""
import hashlib,json,os,subprocess
from pathlib import Path
import httpx
import e2e_realtime_app as fixture
from character_memory.llm.client import OpenAICompatibleModel

fixture.settings.chat_model='deepseek-flash'
model=OpenAICompatibleModel('fixture-key',model=fixture.settings.chat_model,vision_model=fixture.settings.chat_model,attempts=1)
model.client.close()
log=Path(os.environ['MODEL_BUDGET_CALL_LOG'])
def handle(request):
    body=json.loads(request.content)
    assert body['model']=='deepseek-v4.1-flash'
    assert request.url=='https://opencode.ai/zen/go/v1/chat/completions'
    with log.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'model':body['model'],'pid':os.getpid(),'session':request.headers['x-opencode-session'],'external_calls':0})+'\n')
    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'actions':[{'type':'MESSAGE','message':'Compatibility fixture reply.'}]})}}]})
model.client=httpx.Client(transport=httpx.MockTransport(handle))
fixture.model=model;fixture.bundle.model=model
for runtime in fixture.runtimes.values():runtime.model=model
app=fixture.app
app.router.on_shutdown.append(model.close)

@app.get('/__e2e/provider-model')
def runtime():
    root=Path(__file__).resolve().parents[1]
    paths=['src/character_memory/llm/client.py','src/character_memory/config.py','tests/e2e_provider_retirement_app.py']
    return {'pid':os.getpid(),'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
            'configured_model':fixture.settings.chat_model,'actual_model':model.model,'actual_vision_model':model.vision_model,
            'calls':[json.loads(x) for x in log.read_text(encoding='utf-8').splitlines()] if log.exists() else [],
            'source_sha256':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths},
            'git_status_porcelain':subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).splitlines(),'external_calls':0}