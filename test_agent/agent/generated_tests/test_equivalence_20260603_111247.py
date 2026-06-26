import pytest
import urllib.parse, urllib.error, urllib.request
import gzip, io, json, responses, requests
from unittest.mock import MagicMock, patch, Mock
from core.alert import info, warn, messages
from core.compatible import version

from urllib_test import _update as orig_update, _check as orig_check
from requests_test import _update as mig_update, _check as mig_check

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
def mock_version():
    with patch('core.compatible.version', return_value=3):
        yield

def test_happy_path_update(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    mock_resp = MagicMock()
    mock_resp.read.return_value = f"{payload['version']} {payload['code_name']}\n".encode('utf-8')
    with patch('urllib.request.urlopen', return_value=mock_resp) as mock_urlopen:
        orig_update(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'info' in captured.out

def test_happy_path_check(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    mock_resp = MagicMock()
    mock_resp.read.return_value = f"{payload['version']} {payload['code_name']}\n".encode('utf-8')
    with patch('urllib.request.urlopen', return_value=mock_resp) as mock_urlopen:
        orig_check(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'info' in captured.out

def test_http_error_update(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
    with patch('urllib.request.urlopen', side_effect=mock_resp.read) as mock_urlopen:
        orig_update(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_http_error_check(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
    with patch('urllib.request.urlopen', side_effect=mock_resp.read) as mock_urlopen:
        orig_check(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_network_error_update(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Network error')
    with patch('urllib.request.urlopen', side_effect=mock_resp.read) as mock_urlopen:
        orig_update(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_network_error_check(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Network error')
    with patch('urllib.request.urlopen', side_effect=mock_resp.read) as mock_urlopen:
        orig_check(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_migrated_happy_path_update(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    with patch('requests.get', return_value=Mock(text=f"{payload['version']} {payload['code_name']}", status_code=200)) as mock_get:
        mig_update(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'info' in captured.out

def test_migrated_happy_path_check(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    with patch('requests.get', return_value=Mock(text=f"{payload['version']} {payload['code_name']}", status_code=200)) as mock_get:
        mig_check(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'info' in captured.out

def test_migrated_http_error_update(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    with patch('requests.get', side_effect=requests.exceptions.HTTPError('HTTP error')) as mock_get:
        mig_update(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_migrated_http_error_check(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    with patch('requests.get', side_effect=requests.exceptions.HTTPError('HTTP error')) as mock_get:
        mig_check(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_migrated_network_error_update(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    with patch('requests.get', side_effect=requests.exceptions.ConnectionError('Network error')) as mock_get:
        mig_update(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out

def test_migrated_network_error_check(mock_version, capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    payload = {'version': 'test', 'code_name': 'test'}
    with patch('requests.get', side_effect=requests.exceptions.ConnectionError('Network error')) as mock_get:
        mig_check(payload['version'], payload['code_name'], 'en')
        captured = capsys.readouterr()
        assert 'warn' in captured.out