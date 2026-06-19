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

@pytest.fixture
def mock_urlopen():
    with patch('urllib.request.urlopen') as mock:
        yield mock

@pytest.fixture
def mock_requests_get():
    with patch('requests.get') as mock:
        yield mock

def test_happy_path_update(mock_urlopen, mock_requests_get):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    # Mock original module
    mock_resp = MagicMock()
    mock_resp.read.return_value = f'{version} {code_name}'.encode('utf-8')
    mock_urlopen.return_value = mock_resp

    # Mock migrated module
    mock_resp_mig = Mock()
    mock_resp_mig.text = f'{version} {code_name}'
    mock_requests_get.return_value = mock_resp_mig

    orig_update(version, code_name, language)
    mig_update(version, code_name, language)

def test_http_error_check(mock_urlopen, mock_requests_get):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    # Mock original module
    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
    mock_urlopen.return_value = mock_resp

    # Mock migrated module
    mock_resp_mig = Mock()
    mock_resp_mig.status_code = 404
    mock_requests_get.return_value = mock_resp_mig

    with pytest.warns(UserWarning):
        orig_check(version, code_name, language)
        mig_check(version, code_name, language)

def test_network_error_update(mock_urlopen, mock_requests_get):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    # Mock original module
    mock_resp = MagicMock()
    mock_resp.read.side_effect = urllib.error.URLError('Connection error')
    mock_urlopen.return_value = mock_resp

    # Mock migrated module
    mock_requests_get.side_effect = requests.exceptions.ConnectionError()

    with pytest.warns(UserWarning):
        orig_update(version, code_name, language)
        mig_update(version, code_name, language)

def test_redirect_detection(mock_requests_get):
    version = '1.0'
    code_name = 'test'
    language = 'en'

    # Mock migrated module
    mock_resp_mig = Mock()
    mock_resp_mig.text = f'{version} {code_name}'
    mock_resp_mig.history = ['redirected']
    mock_requests_get.return_value = mock_resp_mig

    with pytest.warns(UserWarning):
        mig_update(version, code_name, language)

def test_custom_headers(mock_requests_get):
    version = '1.0'
    code_name = 'test'
    language = 'en'

    # Mock migrated module
    mock_resp_mig = Mock()
    mock_resp_mig.text = f'{version} {code_name}'
    mock_requests_get.return_value = mock_resp_mig

    mig_update(version, code_name, language, headers={'User-Agent': 'test'})

@responses.activate
def test_responses_library():
    version = '1.0'
    code_name = 'test'
    language = 'en'
    url = 'http://nettacker.z3r0d4y.com/version.py'

    responses.add(responses.GET, url,
                  body=f'{version} {code_name}',
                  status=200)

    mig_check(version, code_name, language)

def test_response_parsing_error(mock_urlopen, mock_requests_get):
    url = 'http://nettacker.z3r0d4y.com/version.py'
    version = '1.0'
    code_name = 'test'
    language = 'en'

    # Mock original module
    mock_resp = MagicMock()
    mock_resp.read.return_value = 'invalid'.encode('utf-8')
    mock_urlopen.return_value = mock_resp

    # Mock migrated module
    mock_resp_mig = Mock()
    mock_resp_mig.text = 'invalid'
    mock_requests_get.return_value = mock_resp_mig

    with pytest.warns(UserWarning):
        orig_check(version, code_name, language)
        mig_check(version, code_name, language)