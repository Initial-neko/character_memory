"""Explicit provider-native protocol experiment; does not enable runtime tools."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit


def inspect_native_response(response):
    """Validate only the probe's pure integer-addition calls, never arbitrary tools."""
    try:
        message=response['choices'][0]['message']
    except (KeyError,IndexError,TypeError):
        raise ValueError('INVALID_COMPLETION') from None
    calls=message.get('tool_calls')
    if not isinstance(calls,list) or not calls:
        raise ValueError('NO_NATIVE_CALLS')
    if len(calls)>3:
        raise ValueError('CALL_LIMIT')
    validated=[]
    identifiers=set()
    for call in calls:
        if not isinstance(call,dict) or call.get('type')!='function':
            raise ValueError('INVALID_CALL')
        identifier=call.get('id')
        if not isinstance(identifier,str) or not identifier or len(identifier)>256 or identifier in identifiers:
            raise ValueError('INVALID_CALL_ID')
        identifiers.add(identifier)
        function=call.get('function',{})
        if not isinstance(function,dict) or function.get('name')!='add_integers':
            raise ValueError('UNKNOWN_TOOL')
        raw=function.get('arguments')
        if not isinstance(raw,str) or len(raw)>1024:
            raise ValueError('INVALID_ARGUMENT_JSON')
        try:
            arguments=json.loads(raw)
        except (ValueError,TypeError):
            raise ValueError('INVALID_ARGUMENT_JSON') from None
        if (not isinstance(arguments,dict) or set(arguments)!={'a','b'}
                or any(type(v) is not int or abs(v)>1000 for v in arguments.values())):
            raise ValueError('INVALID_ARGUMENTS')
        validated.append({'id':identifier,'arguments':arguments,'result':arguments['a']+arguments['b']})
    return {'content_was_null':message.get('content') is None,'calls':validated}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config.yaml')
    parser.add_argument('--output',required=True)
    parser.add_argument('--allow-paid',action='store_true')
    args=parser.parse_args()
    if not args.allow_paid:
        parser.error('one real gateway request requires --allow-paid')
    from character_memory.config import load_settings
    import httpx
    settings=load_settings(args.config)
    if not settings.api_key:
        parser.error('configured API credential is unavailable')
    root=Path(args.output).resolve()
    root.parent.mkdir(parents=True,exist_ok=True)
    payload={'model':settings.chat_model,'temperature':0,'max_tokens':256,
        'messages':[{'role':'user','content':'Use add_integers to compute 17 plus 28. Return the function call.'}],
        'tools':[{'type':'function','function':{'name':'add_integers','description':'Probe-only pure integer addition.',
            'parameters':{'type':'object','additionalProperties':False,'properties':{
                'a':{'type':'integer','minimum':-1000,'maximum':1000},
                'b':{'type':'integer','minimum':-1000,'maximum':1000}},'required':['a','b']}}}],
        'tool_choice':{'type':'function','function':{'name':'add_integers'}}}
    report={'provider_host':urlsplit(settings.base_url).hostname,'model':settings.chat_model,
        'runtime_protocol':'UNCHANGED_TYPED_JSON','formal_database_opened':False,
        'request_count':1,'probe_schema':payload['tools'][0]['function']['parameters']}
    started=time.monotonic()
    try:
        # One explicit request, no retries, no follow-up tool execution or writes.
        with httpx.Client(timeout=30,follow_redirects=False) as client:
            response=client.post(settings.base_url.rstrip('/')+'/chat/completions',
                headers={'Authorization':'Bearer '+settings.api_key,'Content-Type':'application/json',
                         'User-Agent':'character-memory/0.4','x-opencode-client':'character-memory',
                         'x-opencode-session':str(uuid.uuid4())},json=payload)
        report['http_status']=response.status_code
        if response.status_code!=200:
            report.update(native_status='NOT_CONFIRMED',reason='GATEWAY_REJECTED',
                gateway_error=response.text.replace(settings.api_key,'[REDACTED]')[:1024])
        else:
            raw=response.json()
            report['raw_response']=raw
            report['usage']=raw.get('usage',{})
            try:
                report['inspection']=inspect_native_response(raw)
                report['native_status']='SUPPORTED_PROBE_ONLY'
            except ValueError as exc:
                report.update(native_status='NOT_CONFIRMED',reason=str(exc))
    except Exception as exc:
        report.update(native_status='NOT_CONFIRMED',reason=type(exc).__name__)
    report['duration_seconds']=time.monotonic()-started
    report['decision']='Keep existing typed-JSON runtime; this probe never enables native dispatch or grants.'
    root.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(root),'native_status':report['native_status'],'reason':report.get('reason')}))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
