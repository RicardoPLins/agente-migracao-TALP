import pytest
import json
import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import requests
from unittest.mock import MagicMock, patch, Mock
from config import update_url
from todoist.api import TodoistAPI

# Define the payload
PAYLOAD = {"tag_name": "test_tag", "html_url": "test_url"}

# Mock the TodoistAPI
@pytest.fixture
def todoist_api():
    return TodoistAPI("test_api_key")

# Test the original module
def test_original_module(todoist_api):
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(PAYLOAD).encode('utf-8')
        mock_urlopen.return_value = mock_resp
        try:
            urllib.request.urlopen(update_url)
            mock_urlopen.assert_called_once_with(update_url)
        except Exception as e:
            assert False, f"Unexpected exception: {e}"

# Test the migrated module
def test_migrated_module(todoist_api):
    with patch('requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.json.return_value = [PAYLOAD]
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        try:
            response = requests.get(update_url)
            response.raise_for_status()
            assert response.json() == [PAYLOAD]
        except Exception as e:
            assert False, f"Unexpected exception: {e}"

# Test the original module with gzip
def test_original_module_gzip(todoist_api):
    with patch('urllib.request.urlopen') as mock_urlopen:
        compressed = gzip.compress(json.dumps(PAYLOAD).encode())
        buf = io.BytesIO(compressed)
        mock_resp = MagicMock()
        mock_resp.read.side_effect = buf.read
        mock_resp.readable.return_value = True
        mock_resp.seekable.return_value = False
        mock_resp.writable.return_value = False
        mock_urlopen.return_value = mock_resp
        try:
            urllib.request.urlopen(update_url)
            mock_urlopen.assert_called_once_with(update_url)
        except Exception as e:
            assert False, f"Unexpected exception: {e}"

# Test the original module with network error
def test_original_module_network_error(todoist_api):
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("reason")
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(update_url)

# Test the original module with HTTP error
def test_original_module_http_error(todoist_api):
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.code = 404
        mock_resp.reason = "Not Found"
        mock_urlopen.return_value = mock_resp
        try:
            urllib.request.urlopen(update_url)
            assert False, "Expected urllib.error.HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404

# Test the migrated module with network error
def test_migrated_module_network_error(todoist_api):
    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.get(update_url)

# Test the migrated module with HTTP error
def test_migrated_module_http_error(todoist_api):
    with patch('requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError()
        mock_get.return_value = mock_resp
        with pytest.raises(requests.exceptions.HTTPError):
            requests.get(update_url).raise_for_status()