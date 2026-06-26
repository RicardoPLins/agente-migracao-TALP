import pytest
import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
from unittest.mock import MagicMock, patch, Mock
import requests
from responses import add

from original_module import _update as orig_update, _check as orig_check
from migrated_module import _update as mig_update, _check as mig_check

MODULE_QUIRKS = {
    "original": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": False,
        "raises_on_network_error": False,
    },
    "migrated": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": True,
        "raises_on_network_error": True,
    }
}

@pytest.fixture
def mock_response():
    payload = {"key": "value"}
    return json.dumps(payload).encode('utf-8')

@pytest.fixture
def mock_http_error():
    return urllib.error.HTTPError("url", 404, "msg", {}, None)

@pytest.fixture
def mock_network_error():
    return urllib.error.URLError("reason")

def test_happy_path_update(mock_response):
    with patch('urllib.request.urlopen', return_value=Mock(read=Mock(return_value=mock_response))):
        orig_update("version", "code_name", "language")
    with patch('requests.get', return_value=Mock(text=mock_response.decode('utf-8'))):
        mig_update("version", "code_name", "language")

def test_http_error_update(mock_http_error):
    with patch('urllib.request.urlopen', side_effect=mock_http_error):
        orig_update("version", "code_name", "language")
    with patch('requests.get', side_effect=requests.exceptions.HTTPError()):
        with pytest.raises(requests.exceptions.HTTPError):
            mig_update("version", "code_name", "language")

def test_network_error_update(mock_network_error):
    with patch('urllib.request.urlopen', side_effect=mock_network_error):
        orig_update("version", "code_name", "language")
    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        with pytest.raises(requests.exceptions.ConnectionError):
            mig_update("version", "code_name", "language")

def test_happy_path_check(mock_response):
    with patch('urllib.request.urlopen', return_value=Mock(read=Mock(return_value=mock_response))):
        orig_check("version", "code_name", "language")
    with patch('requests.get', return_value=Mock(text=mock_response.decode('utf-8'))):
        mig_check("version", "code_name", "language")

def test_http_error_check(mock_http_error):
    with patch('urllib.request.urlopen', side_effect=mock_http_error):
        orig_check("version", "code_name", "language")
    with patch('requests.get', side_effect=requests.exceptions.HTTPError()):
        with pytest.raises(requests.exceptions.HTTPError):
            mig_check("version", "code_name", "language")

def test_network_error_check(mock_network_error):
    with patch('urllib.request.urlopen', side_effect=mock_network_error):
        orig_check("version", "code_name", "language")
    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        with pytest.raises(requests.exceptions.ConnectionError):
            mig_check("version", "code_name", "language")