import pytest
import urllib.parse, urllib.error, urllib.request
import gzip, io, json, responses, requests
from unittest.mock import MagicMock, patch, Mock

from original_module import _update as orig_update, _check as orig_check
from migrated_module import _update as mig_update, _check as mig_check

def test_update_happy_path():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    payload = f"{version} {code_name}"
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp):
        orig_update(version, code_name, language)

    with patch('requests.get', return_value=Mock(text=payload)):
        mig_update(version, code_name, language)

def test_update_http_error():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    with patch('urllib.request.urlopen', side_effect=mock_resp.read.side_effect):
        with pytest.warns(UserWarning):
            orig_update(version, code_name, language)

    with patch('requests.get', side_effect=requests.exceptions.HTTPError()):
        with pytest.warns(UserWarning):
            mig_update(version, code_name, language)

def test_update_network_error():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Connection failed')

    with patch('urllib.request.urlopen', side_effect=mock_resp.read.side_effect):
        with pytest.warns(UserWarning):
            orig_update(version, code_name, language)

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        with pytest.warns(UserWarning):
            mig_update(version, code_name, language)

def test_check_happy_path():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    payload = f"{version} {code_name}"
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode('utf-8')

    with patch('urllib.request.urlopen', return_value=mock_resp):
        orig_check(version, code_name, language)

    with patch('requests.get', return_value=Mock(text=payload)):
        mig_check(version, code_name, language)

def test_check_http_error():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    with patch('urllib.request.urlopen', side_effect=mock_resp.read.side_effect):
        with pytest.warns(UserWarning):
            orig_check(version, code_name, language)

    with patch('requests.get', side_effect=requests.exceptions.HTTPError()):
        with pytest.warns(UserWarning):
            mig_check(version, code_name, language)

def test_check_network_error():
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test_code'
    language = 'en'

    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Connection failed')

    with patch('urllib.request.urlopen', side_effect=mock_resp.read.side_effect):
        with pytest.warns(UserWarning):
            orig_check(version, code_name, language)

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        with pytest.warns(UserWarning):
            mig_check(version, code_name, language)