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

from original_module import _update as orig_update, _check as orig_check
from migrated_module import _update as mig_update, _check as mig_check

MODULE_QUIRKS = {
    "original": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": False,
        "raises_on_network_error": False
    },
    "migrated": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": True,
        "raises_on_network_error": True
    }
}

def test_happy_path_update(capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    payload = f"{version} {code_name}"
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp):
        orig_update(version, code_name, language)

    captured = capsys.readouterr()
    assert 'info' in captured.out

    with patch('requests.get', return_value=Mock(text=payload)):
        mig_update(version, code_name, language)

    captured = capsys.readouterr()
    assert 'info' in captured.out

def test_http_error_update(capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    with patch('urllib.request.urlopen', side_effect=mock_resp):
        orig_update(version, code_name, language)

    captured = capsys.readouterr()
    assert 'warn' in captured.out

    with patch('requests.get', side_effect=requests.exceptions.HTTPError('404 Not Found')):
        with pytest.raises(requests.exceptions.HTTPError):
            mig_update(version, code_name, language)

def test_network_error_update(capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Connection refused')

    with patch('urllib.request.urlopen', side_effect=mock_resp):
        orig_update(version, code_name, language)

    captured = capsys.readouterr()
    assert 'warn' in captured.out

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        mig_update(version, code_name, language)

    captured = capsys.readouterr()
    assert 'warn' in captured.out

def test_happy_path_check(capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    payload = f"{version} {code_name}"
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp):
        orig_check(version, code_name, language)

    captured = capsys.readouterr()
    assert 'info' in captured.out

    with patch('requests.get', return_value=Mock(text=payload)):
        mig_check(version, code_name, language)

    captured = capsys.readouterr()
    assert 'info' in captured.out

def test_http_error_check(capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    with patch('urllib.request.urlopen', side_effect=mock_resp):
        orig_check(version, code_name, language)

    captured = capsys.readouterr()
    assert 'warn' in captured.out

    with patch('requests.get', side_effect=requests.exceptions.HTTPError('404 Not Found')):
        with pytest.raises(requests.exceptions.HTTPError):
            mig_check(version, code_name, language)

def test_network_error_check(capsys):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Connection refused')

    with patch('urllib.request.urlopen', side_effect=mock_resp):
        orig_check(version, code_name, language)

    captured = capsys.readouterr()
    assert 'warn' in captured.out

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        mig_check(version, code_name, language)

    captured = capsys.readouterr()
    assert 'warn' in captured.out