import pytest
import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "validate_capability_provider", Path(__file__).resolve().parents[1] / "scripts/validate_capability_provider.py"
)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(probe)
inspect_native_response = probe.inspect_native_response


def completion(*calls, content=None):
    return {'choices':[{'message':{'content':content,'tool_calls':list(calls)}}]}


def call(identifier, arguments, name='add_integers'):
    return {'id':identifier,'type':'function','function':{'name':name,'arguments':arguments}}


def test_native_probe_accepts_null_content_and_multiple_bounded_calls():
    report=inspect_native_response(completion(call('a','{"a":1,"b":2}'),call('b','{"a":3,"b":4}')))
    assert [x['result'] for x in report['calls']]==[3,7]
    assert report['content_was_null'] is True


@pytest.mark.parametrize('response,reason',[
    (completion(call('a','broken')), 'INVALID_ARGUMENT_JSON'),
    (completion(call('a','{"a":1,"b":2,"role_grant":"admin"}')), 'INVALID_ARGUMENTS'),
    (completion(call('a','{"a":true,"b":2}')), 'INVALID_ARGUMENTS'),
    (completion(call('a','{"a":1,"b":2}','shell')), 'UNKNOWN_TOOL'),
    (completion(*[call(str(i),'{"a":1,"b":2}') for i in range(4)]), 'CALL_LIMIT'),
    (completion(content='45'), 'NO_NATIVE_CALLS'),
])
def test_native_probe_fails_closed(response,reason):
    with pytest.raises(ValueError,match=reason):
        inspect_native_response(response)
