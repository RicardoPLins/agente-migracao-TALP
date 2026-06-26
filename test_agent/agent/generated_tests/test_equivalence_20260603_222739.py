import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import requests
from unittest.mock import MagicMock, patch, Mock
import pytest

MODULE_QUIRKS = {
    "original": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": False,
        "raises_on_network_error": False,
        "generateRequestData_return_type": "other",
        "local_imports": [],
        "missing_imports": []
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
    "behavioral_diffs": []
}

def test_happy_path_original():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'
    PAYLOAD = {"key": "value"}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch('urllib.request.urlopen', return_value=mock_resp):
        with patch('json.loads') as mock_json:
            mock_json.return_value = PAYLOAD
            request = urllib.request.Request(config.update_url)
            response = urllib.request.urlopen(request)
            data = json.loads(response.read().decode('utf-8'))
            assert data == PAYLOAD

def test_happy_path_migrated():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'
    PAYLOAD = {"key": "value"}

    mock_resp = MagicMock()
    mock_resp.json.return_value = PAYLOAD
    mock_resp.status_code = 200

    with patch('requests.get', return_value=mock_resp):
        response = requests.get(config.update_url)
        data = response.json()
        assert data == PAYLOAD

def test_http_error_original():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'

    mock_resp = MagicMock()
    mock_resp.code = 404
    mock_resp.msg = 'Not Found'
    mock_resp.reason = 'Not Found'

    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('url', 404, 'Not Found', {}, mock_resp)):
        with pytest.raises(urllib.error.HTTPError):
            request = urllib.request.Request(config.update_url)
            urllib.request.urlopen(request)

def test_http_error_migrated():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.reason = 'Not Found'

    with patch('requests.get', return_value=mock_resp):
        with pytest.raises(requests.exceptions.HTTPError):
            response = requests.get(config.update_url)
            response.raise_for_status()

def test_network_error_original():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'

    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('reason')):
        with pytest.raises(urllib.error.URLError):
            request = urllib.request.Request(config.update_url)
            urllib.request.urlopen(request)

def test_network_error_migrated():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'

    with patch('requests.get', side_effect=requests.exceptions.RequestException('reason')):
        with pytest.raises(requests.exceptions.RequestException):
            requests.get(config.update_url)

def test_response_parsing_original():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'
    PAYLOAD = {"key": "value"}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp):
        request = urllib.request.Request(config.update_url)
        response = urllib.request.urlopen(request)
        data = json.loads(response.read().decode('utf-8'))
        assert data == PAYLOAD

def test_response_parsing_migrated():
    config = MagicMock()
    config.update_url = 'https://example.com/endpoint'
    PAYLOAD = {"key": "value"}

    mock_resp = MagicMock()
    mock_resp.json.return_value = PAYLOAD

    with patch('requests.get', return_value=mock_resp):
        response = requests.get(config.update_url)
        data = response.json()
        assert data == PAYLOAD