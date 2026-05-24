import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import os
import responses
import requests
from unittest.mock import MagicMock, patch
import pytest
from original_module import ConversationScraper as OriginalConversationScraper
from migrated_module import ConversationScraper as MigratedConversationScraper

@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("conversation_scraper_test")

def test_e2e_original(tmp_path):
    conv_id = "12345"
    offset = 0
    chunk_size = 2000
    cookie = "cookie_value"
    fb_dtsg = "fb_dtsg_value"
    user_id = "user_id_value"
    out_dir = str(tmp_path / "orig")

    RESPONSE_DATA = {"payload": {"actions": [{"id": "1", "timestamp": "100"}]}, "end_of_history": True}
    raw = ("X" * 9 + json.dumps(RESPONSE_DATA)).encode()
    compressed = gzip.compress(raw)
    buf = io.BytesIO(compressed)
    mock_resp = MagicMock()
    mock_resp.read.side_effect = buf.read
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        scraper = OriginalConversationScraper(conv_id, offset, chunk_size, cookie, fb_dtsg, user_id, out_dir)
        scraper.scrapeConversation(False)

    orig_result = json.loads((tmp_path / "orig" / conv_id / "conversation.json").read_text())

    return orig_result

def test_e2e_migrated(tmp_path):
    conv_id = "12345"
    cookie = "cookie_value"
    fb_dtsg = "fb_dtsg_value"
    out_dir = str(tmp_path / "mig")

    RESPONSE_DATA = {"payload": {"actions": [{"id": "1", "timestamp": "100"}]}, "end_of_history": True}
    PREFIX = "X" * 9
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=PREFIX + json.dumps(RESPONSE_DATA), status=200)

        scraper = MigratedConversationScraper(conv_id, cookie, fb_dtsg, out_dir)
        scraper.scrapeConversation(False, 0, 0, 2000, 0, False)

    mig_result = json.loads((tmp_path / "mig" / conv_id / "conversation.json").read_text())

    return mig_result

def test_e2e_equivalence(tmp_path):
    orig_result = test_e2e_original(tmp_path)
    mig_result = test_e2e_migrated(tmp_path)
    assert orig_result == mig_result