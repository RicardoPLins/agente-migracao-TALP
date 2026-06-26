import pytest
import urllib.parse, urllib.error, urllib.request
import gzip, io, json, responses, requests
from unittest.mock import MagicMock, patch, Mock

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
    return MagicMock()

def test_happy_path_update(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    payload = f"{version} {code_name}"
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp) as mock_urlopen:
        orig_update(version, code_name, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get') as mock_get:
        mock_get.return_value.text = payload
        mig_update(version, code_name, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_http_error_update(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(url, 404, 'Not Found', {}, None)):
        orig_update(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_get.return_value.status_code = 404
        with pytest.raises(requests.exceptions.HTTPError):
            mig_update(version, code_name, language)

def test_network_error_update(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('Network error')):
        orig_update(version, code_name, language)

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        mig_update(version, code_name, language)

def test_happy_path_check(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    payload = f"{version} {code_name}"
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp) as mock_urlopen:
        orig_check(version, code_name, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get') as mock_get:
        mock_get.return_value.text = payload
        mig_check(version, code_name, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_http_error_check(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(url, 404, 'Not Found', {}, None)):
        orig_check(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_get.return_value.status_code = 404
        with pytest.raises(requests.exceptions.HTTPError):
            mig_check(version, code_name, language)

def test_network_error_check(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('Network error')):
        orig_check(version, code_name, language)

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        mig_check(version, code_name, language)