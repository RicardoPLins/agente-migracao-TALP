import pytest
import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
from unittest.mock import MagicMock, patch, Mock
import requests
from responses import add

from original_module import _update as orig_update, _check as orig_check
from migrated_module import _update as mig_update, _check as mig_check

@pytest.fixture
def mock_url():
    return 'http://nettacker.z3r0d4y.com/version.py'

@pytest.mark.parametrize("func, args", [
    (orig_update, ("__version__", "__code_name__", "language")),
    (orig_check, ("__version__", "__code_name__", "language")),
    (mig_update, ("__version__", "__code_name__", "language")),
    (mig_check, ("__version__", "__code_name__", "language")),
])
def test_happy_path(mock_url, func, args, capsys):
    with patch('urllib.request.urlopen' if func in [orig_update, orig_check] else 'requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.read.return_value = f"__version__ __code_name__\n"
        mock_get.return_value = mock_resp if func in [orig_update, orig_check] else Mock(text=f"__version__ __code_name__\n", status_code=200)
        func(*args)
        captured = capsys.readouterr()
        assert "info" in captured.out

def test_http_error_orig_update(mock_url, capsys):
    with patch('urllib.request.urlopen') as mock_get:
        mock_get.side_effect = urllib.error.HTTPError(mock_url, 404, "Not Found", {}, None)
        orig_update("__version__", "__code_name__", "language")
        captured = capsys.readouterr()
        assert "warn" in captured.out

def test_http_error_mig_update(mock_url, capsys):
    add(responses.GET, mock_url, status=404)
    with patch('builtins.print') as mock_print:
        mig_update("__version__", "__code_name__", "language")
        captured = capsys.readouterr()
        assert "warn" in captured.out

def test_network_error_orig_update(mock_url, capsys):
    with patch('urllib.request.urlopen') as mock_get:
        mock_get.side_effect = urllib.error.URLError("reason")
        orig_update("__version__", "__code_name__", "language")
        captured = capsys.readouterr()
        assert "warn" in captured.out

def test_network_error_mig_update(mock_url, capsys):
    add(responses.GET, mock_url, body=requests.exceptions.ConnectionError())
    with patch('builtins.print') as mock_print:
        mig_update("__version__", "__code_name__", "language")
        captured = capsys.readouterr()
        assert "warn" in captured.out

def test_invalid_input(mock_url):
    with patch('urllib.request.urlopen' if orig_update in [orig_update, orig_check] else 'requests.get') as mock_get:
        mock_get.return_value = Mock(text="", status_code=200)
        orig_update(None, None, None)
        mig_update(None, None, None)

def test_custom_headers(mock_url):
    with patch('urllib.request.urlopen') as mock_get:
        mock_get.return_value = Mock(read=Mock(return_value=b"__version__ __code_name__\n"))
        orig_update("__version__", "__code_name__", "language")
    add(responses.GET, mock_url, text="__version__ __code_name__\n")
    mig_update("__version__", "__code_name__", "language")

def test_response_parsing_error(mock_url, capsys):
    with patch('urllib.request.urlopen') as mock_get:
        mock_get.return_value = Mock(read=Mock(return_value=b"invalid response"))
        orig_update("__version__", "__code_name__", "language")
        captured = capsys.readouterr()
        assert "warn" in captured.out
    add(responses.GET, mock_url, body="invalid response")
    with patch('builtins.print') as mock_print:
        mig_update("__version__", "__code_name__", "language")
        captured = capsys.readouterr()
        assert "warn" in captured.out