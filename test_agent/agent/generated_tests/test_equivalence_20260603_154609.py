import pytest
import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch, Mock
import config

# Import modules to test
import original_module
import migrated_module

MODULE_QUIRKS = {
    "original": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": False,
        "raises_on_network_error": False
    },
    "migrated": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": False,
        "raises_on_network_error": True
    }
}

def test_happy_path():
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False

    with patch.object(urllib.request, 'urlopen', return_value=mock_resp):
        with patch.object(requests, 'get', return_value=Mock(json=lambda: PAYLOAD)):
            original_module.check_for_updates()
            migrated_module.check_for_updates()

def test_http_error():
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.code = 404
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')

    with patch.object(urllib.request, 'urlopen', side_effect=urllib.error.HTTPError("url", 404, "msg", {}, None)):
        with patch.object(requests, 'get', status_code=404):
            with pytest.raises(urllib.error.HTTPError):
                original_module.check_for_updates()
            with pytest.raises(requests.RequestException):
                migrated_module.check_for_updates()

def test_network_error():
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()

    with patch.object(urllib.request, 'urlopen', side_effect=urllib.error.URLError("reason")):
        with patch.object(requests, 'get', side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(urllib.error.URLError):
                original_module.check_for_updates()
            with pytest.raises(requests.RequestException):
                migrated_module.check_for_updates()

def test_response_parsing():
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')

    with patch.object(urllib.request, 'urlopen', return_value=mock_resp):
        with patch.object(requests, 'get', return_value=Mock(json=lambda: PAYLOAD)):
            original_module.check_for_updates()
            migrated_module.check_for_updates()

    # Test invalid JSON
    mock_resp.read.return_value = b'Invalid JSON'
    with patch.object(urllib.request, 'urlopen', return_value=mock_resp):
        with pytest.raises(ValueError):
            original_module.check_for_updates()
    with patch.object(requests, 'get', return_value=Mock(text='Invalid JSON')):
        with pytest.raises(KeyError):
            migrated_module.check_for_updates()

@responses.activate
def test_requests_get():
    responses.add(responses.GET, config.update_url,
                  json=[{"tag_name": "latest"}], status=200)
    migrated_module.check_for_updates()

@responses.activate
def test_requests_get_network_error():
    responses.add(responses.GET, config.update_url,
                  body=requests.exceptions.ConnectionError(), status=500)
    with pytest.raises(requests.RequestException):
        migrated_module.check_for_updates()

@responses.activate
def test_requests_get_http_error():
    responses.add(responses.GET, config.update_url,
                  status=404)
    with pytest.raises(requests.RequestException):
        migrated_module.check_for_updates()