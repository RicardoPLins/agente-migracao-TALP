import pytest
import urllib.parse, urllib.error, urllib.request
import gzip, io, json, responses, requests
from unittest.mock import MagicMock, patch, Mock
from core.alert import info, warn, messages
from core.compatible import version

from original_module import _update as orig_update, _check as orig_check
from migrated_module import _update as mig_update, _check as mig_check

@pytest.fixture
def mock_response():
    PAYLOAD = {"key": "value"}
    return json.dumps(PAYLOAD).encode('utf-8')

@pytest.fixture
def mock_gzip_response():
    PAYLOAD = {"key": "value"}
    compressed = gzip.compress(json.dumps(PAYLOAD).encode())
    return compressed

@responses.activate
def test_update_happy_path(mock_response):
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=mock_response, status=200)
    orig_update('version', 'code_name', 'en')
    mig_update('version', 'code_name', 'en')

@responses.activate
def test_update_http_error(mock_response):
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=mock_response, status=404)
    with patch.object(warn, 'called') as mock_warn:
        orig_update('version', 'code_name', 'en')
        mig_update('version', 'code_name', 'en')
        mock_warn.assert_called()

@responses.activate
def test_update_network_error():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=requests.exceptions.ConnectionError())
    with patch.object(warn, 'called') as mock_warn:
        orig_update('version', 'code_name', 'en')
        mig_update('version', 'code_name', 'en')
        mock_warn.assert_called()

@patch('urllib.request.urlopen')
def test_orig_update_happy_path(mock_urlopen, mock_response):
    mock_urlopen.return_value.read.return_value = mock_response
    orig_update('version', 'code_name', 'en')

@patch('urllib.request.urlopen')
def test_orig_update_http_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.HTTPError('url', 404, 'msg', {}, None)
    with patch.object(warn, 'called') as mock_warn:
        orig_update('version', 'code_name', 'en')
        mock_warn.assert_called()

@patch('urllib.request.urlopen')
def test_orig_update_network_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError('reason')
    with patch.object(warn, 'called') as mock_warn:
        orig_update('version', 'code_name', 'en')
        mock_warn.assert_called()

def test_mig_update_happy_path():
    with patch('requests.get') as mock_get:
        mock_response = Mock()
        mock_response.text = 'version code_name'
        mock_get.return_value = mock_response
        mig_update('version', 'code_name', 'en')

def test_mig_update_http_error():
    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.HTTPError('msg')
        with patch.object(warn, 'called') as mock_warn:
            mig_update('version', 'code_name', 'en')
            mock_warn.assert_called()

def test_mig_update_network_error():
    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()
        with patch.object(warn, 'called') as mock_warn:
            mig_update('version', 'code_name', 'en')
            mock_warn.assert_called()

# check functions
@responses.activate
def test_check_happy_path(mock_response):
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=mock_response, status=200)
    orig_check('version', 'code_name', 'en')
    mig_check('version', 'code_name', 'en')

@responses.activate
def test_check_http_error(mock_response):
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=mock_response, status=404)
    with patch.object(warn, 'called') as mock_warn:
        orig_check('version', 'code_name', 'en')
        mig_check('version', 'code_name', 'en')
        mock_warn.assert_called()

@responses.activate
def test_check_network_error():
    responses.add(responses.GET, 'http://nettacker.z3r0d4y.com/version.py',
                  body=requests.exceptions.ConnectionError())
    with patch.object(warn, 'called') as mock_warn:
        orig_check('version', 'code_name', 'en')
        mig_check('version', 'code_name', 'en')
        mock_warn.assert_called()

@patch('urllib.request.urlopen')
def test_orig_check_happy_path(mock_urlopen, mock_response):
    mock_urlopen.return_value.read.return_value = mock_response
    orig_check('version', 'code_name', 'en')

@patch('urllib.request.urlopen')
def test_orig_check_http_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.HTTPError('url', 404, 'msg', {}, None)
    with patch.object(warn, 'called') as mock_warn:
        orig_check('version', 'code_name', 'en')
        mock_warn.assert_called()

@patch('urllib.request.urlopen')
def test_orig_check_network_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError('reason')
    with patch.object(warn, 'called') as mock_warn:
        orig_check('version', 'code_name', 'en')
        mock_warn.assert_called()

def test_mig_check_happy_path():
    with patch('requests.get') as mock_get:
        mock_response = Mock()
        mock_response.text = 'version code_name'
        mock_get.return_value = mock_response
        mig_check('version', 'code_name', 'en')

def test_mig_check_http_error():
    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.HTTPError('msg')
        with patch.object(warn, 'called') as mock_warn:
            mig_check('version', 'code_name', 'en')
            mock_warn.assert_called()

def test_mig_check_network_error():
    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()
        with patch.object(warn, 'called') as mock_warn:
            mig_check('version', 'code_name', 'en')
            mock_warn.assert_called()