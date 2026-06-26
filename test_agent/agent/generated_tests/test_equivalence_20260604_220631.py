import pytest
import urllib.parse, urllib.error, urllib.request
import gzip, io, json, responses, requests
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
    },
    "migrated": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": True,
        "raises_on_network_error": True,
    }
}

def test_equivalence_update(monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, 'urlopen', MagicMock(return_value=io.BytesIO(json.dumps({"version": "1.0"}).encode())))
    caplog.set_level("INFO")
    orig_update("1.0", "test", "en")
    mig_update("1.0", "test", "en")
    assert "info" in caplog.text

def test_equivalence_check(monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, 'urlopen', MagicMock(return_value=io.BytesIO(json.dumps({"version": "1.0"}).encode())))
    caplog.set_level("INFO")
    orig_check("1.0", "test", "en")
    mig_check("1.0", "test", "en")
    assert "info" in caplog.text

@responses.activate
def test_happy_path_update():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                   body=json.dumps({"version": "1.0"}), status=200)
    mig_update("1.0", "test", "en")
    orig_update("1.0", "test", "en")

@responses.activate
def test_http_error_update():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                   status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig_update("1.0", "test", "en")
    orig_update("1.0", "test", "en")

@responses.activate
def test_network_error_update():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                   body=requests.exceptions.ConnectionError())
    with pytest.raises(requests.exceptions.RequestException):
        mig_update("1.0", "test", "en")
    orig_update("1.0", "test", "en")

@responses.activate
def test_happy_path_check():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                   body=json.dumps({"version": "1.0"}), status=200)
    mig_check("1.0", "test", "en")
    orig_check("1.0", "test", "en")

@responses.activate
def test_http_error_check():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                   status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig_check("1.0", "test", "en")
    orig_check("1.0", "test", "en")

@responses.activate
def test_network_error_check():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                   body=requests.exceptions.ConnectionError())
    with pytest.raises(requests.exceptions.RequestException):
        mig_check("1.0", "test", "en")
    orig_check("1.0", "test", "en")