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

# Import original and migrated modules
import original_module as orig
import migrated_module as mig

# Define test data
url = 'http://nettacker.z3r0d4y.com/version.py'
PAYLOAD = {"key": "value"}

# Define test functions
@pytest.fixture
def mock_response():
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp

def test_happy_path_update():
    with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
        orig._update('version', 'code_name', 'language')
        mock_urlopen.assert_called_once_with(url)

def test_happy_path_check():
    with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
        orig._check('version', 'code_name', 'language')
        mock_urlopen.assert_called_once_with(url)

def test_http_error_update():
    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(url, 404, 'Not Found', {}, None)):
        orig._update('version', 'code_name', 'language')

def test_http_error_check():
    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(url, 404, 'Not Found', {}, None)):
        orig._check('version', 'code_name', 'language')

def test_network_error_update():
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('Network error')):
        orig._update('version', 'code_name', 'language')

def test_network_error_check():
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('Network error')):
        orig._check('version', 'code_name', 'language')

# Migrated module tests
@responses.activate
def test_happy_path_update_migrated():
    responses.add(responses.GET, url, json=PAYLOAD, status=200)
    mig._update('version', 'code_name', 'language')
    assert responses.calls[0].request.url == url

@responses.activate
def test_happy_path_check_migrated():
    responses.add(responses.GET, url, json=PAYLOAD, status=200)
    mig._check('version', 'code_name', 'language')
    assert responses.calls[0].request.url == url

@responses.activate
def test_http_error_update_migrated():
    responses.add(responses.GET, url, status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig._update('version', 'code_name', 'language')

@responses.activate
def test_http_error_check_migrated():
    responses.add(responses.GET, url, status=404)
    with pytest.raises(requests.exceptions.HTTPError):
        mig._check('version', 'code_name', 'language')

@responses.activate
def test_network_error_update_migrated():
    responses.add(responses.GET, url, status=500, body=requests.exceptions.ConnectionError())
    mig._update('version', 'code_name', 'language')

@responses.activate
def test_network_error_check_migrated():
    responses.add(responses.GET, url, status=500, body=requests.exceptions.ConnectionError())
    mig._check('version', 'code_name', 'language')

# Equivalence tests
def test_equivalence_update(capsys):
    with patch('urllib.request.urlopen', return_value=mock_response):
        orig._update('version', 'code_name', 'language')
    with responses.activate:
        responses.add(responses.GET, url, json=PAYLOAD, status=200)
        mig._update('version', 'code_name', 'language')
    captured = capsys.readouterr()
    assert captured.out == captured.out

def test_equivalence_check(capsys):
    with patch('urllib.request.urlopen', return_value=mock_response):
        orig._check('version', 'code_name', 'language')
    with responses.activate:
        responses.add(responses.GET, url, json=PAYLOAD, status=200)
        mig._check('version', 'code_name', 'language')
    captured = capsys.readouterr()
    assert captured.out == captured.out