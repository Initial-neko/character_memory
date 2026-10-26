import os
import pytest
from test_p0_17b_playwright import realtime_server, _open
pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT')!='1',reason='explicit browser runtime')]


def test_suggestions_are_explicit_and_do_not_generate_on_open(page,realtime_server):
    from playwright.sync_api import expect
    requests=[]
    page.on('request',lambda r:requests.append(r) if r.url.endswith('/v2/reply-suggestions') and r.method=='POST' else None)
    _open(page,realtime_server)
    expect(page.get_by_role('button',name='帮我想想',exact=True)).to_be_visible()
    assert not requests
