import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch
import pytest
from original_module import ConversationScraper as OriginalConversationScraper
from migrated_module import ConversationScraper as MigratedConversationScraper

@pytest.fixture
def original_scraper():
    return OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

@pytest.fixture
def migrated_scraper():
    return MigratedConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

def test_generateRequestData(original_scraper, migrated_scraper):
    original_data = original_scraper.generateRequestData()
    migrated_data = migrated_scraper.generateRequestData()
    original_parsed = urllib.parse.parse_qs(original_data.decode('utf-8'))
    assert original_parsed == migrated_data

def test_executeRequest_original(original_scraper):
    PAYLOAD = {"key": "value"}
    compressed = gzip.compress(json.dumps(PAYLOAD).encode())
    buf = io.BytesIO(compressed)
    mock_resp = MagicMock()
    mock_resp.read.side_effect = buf.read
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch('urllib.request.urlopen', return_value=mock_resp):
        result = original_scraper.executeRequest(original_scraper.generateRequestData())
        assert json.loads(result) == PAYLOAD

def test_executeRequest_migrated(migrated_scraper):
    PAYLOAD = {"key": "value"}
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 json=PAYLOAD, status=200)
        result = migrated_scraper.executeRequest(migrated_scraper.generateRequestData())
        assert json.loads(result) == PAYLOAD

def test_executeRequest_original_network_error(original_scraper):
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("reason")):
        with pytest.raises(urllib.error.URLError):
            original_scraper.executeRequest(original_scraper.generateRequestData())

def test_executeRequest_migrated_network_error(migrated_scraper):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=requests.exceptions.ConnectionError())
        with pytest.raises(requests.exceptions.ConnectionError):
            migrated_scraper.executeRequest(migrated_scraper.generateRequestData())

def test_executeRequest_original_http_error(original_scraper):
    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError("url", 404, "msg", {}, None)):
        result = original_scraper.executeRequest(original_scraper.generateRequestData())
        assert result is not None

def test_executeRequest_migrated_http_error(migrated_scraper):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 status=404)
        result = migrated_scraper.executeRequest(migrated_scraper.generateRequestData())
        assert result is not None