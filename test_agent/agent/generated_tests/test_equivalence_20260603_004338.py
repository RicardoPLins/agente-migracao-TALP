import pytest
import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
from unittest.mock import MagicMock, patch
from core.alert import info, warn, messages
from core.compatible import version
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

def test_happy_path_update(mock_urlopen):
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_urlopen.return_value = mock_resp
    orig_update('version', 'code_name', 'language')
    mig_update('version', 'code_name', 'language')

def test_http_error_update(mock_urlopen):
    mock_resp = urllib.error.HTTPError('url', 404, 'msg', {}, None)
    mock_urlopen.side_effect = mock_resp
    orig_update('version', 'code_name', 'language')
    with pytest.raises(urllib.error.HTTPError):
        mig_update('version', 'code_name', 'language')

def test_network_error_update(mock_urlopen):
    mock_resp = urllib.error.URLError('reason')
    mock_urlopen.side_effect = mock_resp
    orig_update('version', 'code_name', 'language')
    with pytest.raises(urllib.error.URLError):
        mig_update('version', 'code_name', 'language')

def test_happy_path_check(mock_urlopen):
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_urlopen.return_value = mock_resp
    orig_check('version', 'code_name', 'language')
    mig_check('version', 'code_name', 'language')

def test_http_error_check(mock_urlopen):
    mock_resp = urllib.error.HTTPError('url', 404, 'msg', {}, None)
    mock_urlopen.side_effect = mock_resp
    orig_check('version', 'code_name', 'language')
    with pytest.raises(urllib.error.HTTPError):
        mig_check('version', 'code_name', 'language')

def test_network_error_check(mock_urlopen):
    mock_resp = urllib.error.URLError('reason')
    mock_urlopen.side_effect = mock_resp
    orig_check('version', 'code_name', 'language')
    with pytest.raises(urllib.error.URLError):
        mig_check('version', 'code_name', 'language')

def test_response_parsing_update(mock_urlopen):
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_urlopen.return_value = mock_resp
    orig_update('version', 'code_name', 'language')
    mig_update('version', 'code_name', 'language')

def test_response_parsing_check(mock_urlopen):
    PAYLOAD = {"key": "value"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
    mock_urlopen.return_value = mock_resp
    orig_check('version', 'code_name', 'language')
    mig_check('version', 'code_name', 'language')