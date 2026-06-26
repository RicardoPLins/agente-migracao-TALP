import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch, Mock
import pytest

from original_module import _update as orig_update
from original_module import _check as orig_check
from migrated_module import _update as mig_update
from migrated_module import _check as mig_check

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

def test_happy_path_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    payload = f"{version} {code_name}"

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload.encode('utf-8')
        mock_urlopen.return_value = mock_resp

        orig_update(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = payload
        mock_get.return_value = mock_resp

        mig_update(version, code_name, language)

def test_happy_path_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    payload = f"{version} {code_name}"

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload.encode('utf-8')
        mock_urlopen.return_value = mock_resp

        orig_check(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = payload
        mock_get.return_value = mock_resp

        mig_check(version, code_name, language)

def test_http_error_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        orig_update(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        with pytest.raises(requests.exceptions.HTTPError):
            mig_update(version, code_name, language)

def test_http_error_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        orig_check(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        with pytest.raises(requests.exceptions.HTTPError):
            mig_check(version, code_name, language)

def test_network_error_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError('Network error')

        orig_update(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()

        mig_update(version, code_name, language)

def test_network_error_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError('Network error')

        orig_check(version, code_name, language)

    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()

        mig_check(version, code_name, language)