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

MODULE_QUIRKS = {
  "original": {
    "uses_gzip": False,
    "response_strip_chars": 0,
    "raises_on_http_error": False,
    "raises_on_network_error": False,
    "generateRequestData_return_type": "other",
    "local_imports": [],
    "missing_imports": [
      "requests"
    ]
  },
  "migrated": {
    "uses_gzip": False,
    "response_strip_chars": 0,
    "raises_on_http_error": True,
    "raises_on_network_error": True,
    "generateRequestData_return_type": "other",
    "local_imports": [],
    "missing_imports": []
  },
  "behavioral_diffs": [
    "raises_on_http_error",
    "raises_on_network_error"
  ]
}

def generate_payload():
    payload = {"key": "value"}
    return json.dumps(payload).encode('utf-8')

@pytest.fixture
def mock_response():
    mock_resp = MagicMock()
    mock_resp.read.return_value = generate_payload()
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp

@pytest.fixture
def mock_gzip_response():
    payload = {"key": "value"}
    compressed = gzip.compress(json.dumps(payload).encode())
    buf = io.BytesIO(compressed)
    mock_resp = MagicMock()
    mock_resp.read.side_effect = buf.read
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp

@responses.activate
def test_executeRequest_happy_path():
    responses.add(responses.GET, 'https://example.com/endpoint',
                  body=generate_payload(), status=200)
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    original_response = original_scraper.executeRequest('https://example.com/endpoint')
    migrated_response = migrated_scraper.executeRequest('https://example.com/endpoint')

    assert original_response == migrated_response

@responses.activate
def test_executeRequest_http_error():
    responses.add(responses.GET, 'https://example.com/endpoint',
                  status=404)
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    with pytest.raises(urllib.error.HTTPError):
        original_scraper.executeRequest('https://example.com/endpoint')

    with pytest.raises(requests.exceptions.HTTPError):
        migrated_scraper.executeRequest('https://example.com/endpoint')

@responses.activate
def test_executeRequest_network_error():
    responses.add(responses.GET, 'https://example.com/endpoint',
                  body=requests.exceptions.ConnectionError())
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    with pytest.raises(urllib.error.URLError):
        original_scraper.executeRequest('https://example.com/endpoint')

    with pytest.raises(requests.exceptions.ConnectionError):
        migrated_scraper.executeRequest('https://example.com/endpoint')

def test_generateRequestData():
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    original_data = original_scraper.generateRequestData()
    migrated_data = migrated_scraper.generateRequestData()

    assert urllib.parse.urlencode(migrated_data) == original_data.decode('utf-8')

def test_init():
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    assert hasattr(original_scraper, 'executeRequest')
    assert hasattr(migrated_scraper, 'executeRequest')