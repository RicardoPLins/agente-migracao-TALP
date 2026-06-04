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
from core.alert import info, warn, messages
from core.compatible import version

from original_module import _update as orig_update, _check as orig_check
from migrated_module import _update as mig_update, _check as mig_check

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
    "behavioral_diffs": []
}

@pytest.fixture
def mock_response():
    return MagicMock()

@pytest.fixture
def mock_requests_response():
    return MagicMock()

def test_happy_path_update(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    __version__ = '1.0'
    __code_name__ = 'test'
    language = 'en'

    mock_response = MagicMock()
    mock_response.read.return_value = f'{__version__} {__code_name__}'.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
        orig_update(__version__, __code_name__, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get', return_value=mock_requests_response) as mock_get:
        mock_requests_response.text = f'{__version__} {__code_name__}'
        mig_update(__version__, __code_name__, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_http_error_update(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    __version__ = '1.0'
    __code_name__ = 'test'
    language = 'en'

    mock_response = MagicMock()
    mock_response.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    with patch('urllib.request.urlopen', side_effect=mock_response.read) as mock_urlopen:
        orig_update(__version__, __code_name__, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get', side_effect=requests.exceptions.HTTPError('404 Client Error: Not Found')) as mock_get:
        with pytest.raises(requests.exceptions.HTTPError):
            mig_update(__version__, __code_name__, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_network_error_update(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    __version__ = '1.0'
    __code_name__ = 'test'
    language = 'en'

    mock_response = MagicMock()
    mock_response.read.side_effect = urllib.error.URLError('Connection failed')

    with patch('urllib.request.urlopen', side_effect=mock_response.read) as mock_urlopen:
        orig_update(__version__, __code_name__, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError('Connection failed')) as mock_get:
        mig_update(__version__, __code_name__, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_happy_path_check(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    __version__ = '1.0'
    __code_name__ = 'test'
    language = 'en'

    mock_response = MagicMock()
    mock_response.read.return_value = f'{__version__} {__code_name__}'.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
        orig_check(__version__, __code_name__, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get', return_value=mock_requests_response) as mock_get:
        mock_requests_response.text = f'{__version__} {__code_name__}'
        mig_check(__version__, __code_name__, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_http_error_check(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    __version__ = '1.0'
    __code_name__ = 'test'
    language = 'en'

    mock_response = MagicMock()
    mock_response.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    with patch('urllib.request.urlopen', side_effect=mock_response.read) as mock_urlopen:
        orig_check(__version__, __code_name__, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get', side_effect=requests.exceptions.HTTPError('404 Client Error: Not Found')) as mock_get:
        if MODULE_QUIRKS['migrated']['raises_on_http_error']:
            with pytest.raises(requests.exceptions.HTTPError):
                mig_check(__version__, __code_name__, language)
        else:
            mig_check(__version__, __code_name__, language)
        mock_get.assert_called_once_with(url, timeout=5)

def test_network_error_check(monkeypatch):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    __version__ = '1.0'
    __code_name__ = 'test'
    language = 'en'

    mock_response = MagicMock()
    mock_response.read.side_effect = urllib.error.URLError('Connection failed')

    with patch('urllib.request.urlopen', side_effect=mock_response.read) as mock_urlopen:
        orig_check(__version__, __code_name__, language)
        mock_urlopen.assert_called_once_with(url)

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError('Connection failed')) as mock_get:
        if MODULE_QUIRKS['migrated']['raises_on_network_error']:
            with pytest.raises(requests.exceptions.ConnectionError):
                mig_check(__version__, __code_name__, language)
        else:
            mig_check(__version__, __code_name__, language)
        mock_get.assert_called_once_with(url, timeout=5)