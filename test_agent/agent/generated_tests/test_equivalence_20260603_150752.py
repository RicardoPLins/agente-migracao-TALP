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
        "missing_imports": ["requests"]
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

def test_happy_path_update():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"version": "1.0.0"}).encode('utf-8')
        mock_urlopen.return_value = mock_resp
        orig_update("1.0.0", "test", "en")
        mig_update("1.0.0", "test", "en")

def test_happy_path_check():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"version": "1.0.0"}).encode('utf-8')
        mock_urlopen.return_value = mock_resp
        orig_check("1.0.0", "test", "en")
        mig_check("1.0.0", "test", "en")

def test_http_error_update():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("http://example.com", 404, "Not Found", {}, None)
        with pytest.raises(Exception):
            orig_update("1.0.0", "test", "en")
        with pytest.raises(requests.exceptions.HTTPError):
            mig_update("1.0.0", "test", "en")

def test_http_error_check():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("http://example.com", 404, "Not Found", {}, None)
        with pytest.raises(Exception):
            orig_check("1.0.0", "test", "en")
        with pytest.raises(requests.exceptions.HTTPError):
            mig_check("1.0.0", "test", "en")

def test_network_error_update():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("Network error")
        orig_update("1.0.0", "test", "en")
        with pytest.raises(requests.exceptions.ConnectionError):
            mig_update("1.0.0", "test", "en")

def test_network_error_check():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("Network error")
        orig_check("1.0.0", "test", "en")
        with pytest.raises(requests.exceptions.ConnectionError):
            mig_check("1.0.0", "test", "en")

@responses.activate
def test_happy_path_update_requests():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=json.dumps({"version": "1.0.0"}), status=200)
    mig_update("1.0.0", "test", "en")

@responses.activate
def test_happy_path_check_requests():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=json.dumps({"version": "1.0.0"}), status=200)
    mig_check("1.0.0", "test", "en")

@responses.activate
def test_http_error_update_requests():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig_update("1.0.0", "test", "en")

@responses.activate
def test_http_error_check_requests():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig_check("1.0.0", "test", "en")

@responses.activate
def test_network_error_update_requests():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=requests.exceptions.ConnectionError())
    with pytest.raises(requests.exceptions.ConnectionError):
        mig_update("1.0.0", "test", "en")

@responses.activate
def test_network_error_check_requests():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=requests.exceptions.ConnectionError())
    with pytest.raises(requests.exceptions.ConnectionError):
        mig_check("1.0.0", "test", "en")