import urllib.parse, urllib.error, urllib.request
import gzip, io, json, responses, requests
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
        "generateRequestData_return_type": "str",
        "local_imports": [],
        "missing_imports": []
    },
    "behavioral_diffs": []
}

@pytest.fixture
def mock_response():
    return MagicMock()

@responses.activate
def test_happy_path_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = f"mock_version mock_code_name"
    responses.add(responses.GET, url, body=payload, status=200)
    orig_update("mock_version", "mock_code_name", "en")
    mig_update("mock_version", "mock_code_name", "en")

@responses.activate
def test_http_error_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    responses.add(responses.GET, url, status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig_update("mock_version", "mock_code_name", "en")
    orig_update("mock_version", "mock_code_name", "en")

@responses.activate
def test_network_error_update():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    responses.add(responses.GET, url, body=requests.exceptions.ConnectionError())
    with pytest.raises(requests.exceptions.RequestException):
        mig_update("mock_version", "mock_code_name", "en")
    orig_update("mock_version", "mock_code_name", "en")

@responses.activate
def test_happy_path_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = f"mock_version mock_code_name"
    responses.add(responses.GET, url, body=payload, status=200)
    orig_check("mock_version", "mock_code_name", "en")
    mig_check("mock_version", "mock_code_name", "en")

@responses.activate
def test_http_error_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    responses.add(responses.GET, url, status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig_check("mock_version", "mock_code_name", "en")
    orig_check("mock_version", "mock_code_name", "en")

@responses.activate
def test_network_error_check():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    responses.add(responses.GET, url, body=requests.exceptions.ConnectionError())
    with pytest.raises(requests.exceptions.RequestException):
        mig_check("mock_version", "mock_code_name", "en")
    orig_check("mock_version", "mock_code_name", "en")