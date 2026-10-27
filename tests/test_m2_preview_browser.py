"""The opt-in manual preview must preserve the complete real UI loop."""
import os
import pytest
from test_m2_full_loop_browser import test_m2_full_ui_story_survives_refresh_and_actual_restart as _story
pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT') != '1', reason='explicit browser runtime')]

@pytest.mark.parametrize('width,height', [(1280,900),(390,844)])
def test_isolated_preview_full_ui_story(page,tmp_path,monkeypatch,width,height):
    monkeypatch.setenv('M2_PREVIEW_MODE','1')
    _story(page,tmp_path,monkeypatch,width,height)