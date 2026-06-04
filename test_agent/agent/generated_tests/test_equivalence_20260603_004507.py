import pytest
import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import requests
from unittest.mock import MagicMock, patch, Mock
from core.alert import info
from core.alert import warn
from core.alert import messages
from core.compatible import version
import original_module
import migrated_module

MODULE_QUIRKS = {
  "original": {
    "uses_gzip": False,
    "response_strip_chars": 0,
    "raises_on_http_error": False,
    "raises_on_network_error": False,
    "generateRequestData_return_type": "other",
    "local_imports": [
      "core.alert.info",
      "core.alert.warn",
      "core.alert.messages",
      "core.compatible.version"
    ],
    "missing_imports": []
  },
  "migrated": {
    "uses_gzip": False,
    "response_strip_chars": 0,
    "raises_on_http_error": True,
    "raises_on_network_error": True,
    "generateRequestData_return_type": "other",
    "local_imports": [
      "core.alert.info",
      "core.alert.warn",
      "core.alert.messages",
      "requests"
    ],
    "missing_imports": []
  },
  "behavioral_diffs": [
    "raises_on_http_error",
    "raises_on_network_error"
  ]
}

def test_happy_path_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    PAYLOAD = {"key": "value"}
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
        mock_urlopen.return_value = mock_resp
        original_module._update('version', 'code_name', 'language')
        assert info.called

    with patch('requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.text = json.dumps(PAYLOAD)
        mock_get.return_value = mock_resp
        migrated_module._update('version', 'code_name', 'language')
        assert info.called

def test_http_error_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        original_module._update('version', 'code_name', 'language')
        assert warn.called

    with patch('requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        with pytest.raises(requests.exceptions.HTTPError):
            migrated_module._update('version', 'code_name', 'language')

def test_network_error_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError('reason')
        original_module._update('version', 'code_name', 'language')
        assert warn.called

    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()
        with pytest.raises(requests.exceptions.RequestException):
            migrated_module._update('version', 'code_name', 'language')

def test_happy_path_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    PAYLOAD = {"key": "value"}
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
        mock_urlopen.return_value = mock_resp
        original_module._check('version', 'code_name', 'language')
        assert info.called

    with patch('requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.text = json.dumps(PAYLOAD)
        mock_get.return_value = mock_resp
        migrated_module._check('version', 'code_name', 'language')
        assert info.called

def test_http_error_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        original_module._check('version', 'code_name', 'language')
        assert warn.called

    with patch('requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        with pytest.raises(requests.exceptions.HTTPError):
            migrated_module._check('version', 'code_name', 'language')

def test_network_error_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError('reason')
        original_module._check('version', 'code_name', 'language')
        assert warn.called

    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()
        with pytest.raises(requests.exceptions.RequestException):
            migrated_module._check('version', 'code_name', 'language')