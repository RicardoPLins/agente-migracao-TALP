import pytest
import json
import config
from unittest.mock import patch, MagicMock
from original_module import urllib
from migrated_module import requests

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
        "raises_on_http_error": False,
        "raises_on_network_error": True
    }
}

def test_update_check_happy_path():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([{'tag_name': 'v1.0', 'html_url': 'https://example.com'}]).encode('utf-8')
        mock_urlopen.return_value = mock_response

        with patch('migrated_module.requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.text = json.dumps([{'tag_name': 'v1.0', 'html_url': 'https://example.com'}])
            mock_get.return_value = mock_response

            original_update_releaseinforaw = urllib.request.urlopen(config.update_url).read()
            original_json = json.loads(original_update_releaseinforaw.decode('utf-8'))

            migrated_response = requests.get(config.update_url)
            migrated_json_data = json.loads(migrated_response.text)

            assert original_json[0]['tag_name'] == migrated_json_data[0]['tag_name']
            assert original_json[0]['html_url'] == migrated_json_data[0]['html_url']

def test_update_check_http_error():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError('https://example.com', 404, 'Not Found', None, None)

        with patch('migrated_module.requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_get.return_value = mock_response

            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(config.update_url)

            with pytest.raises(requests.RequestException):
                requests.get(config.update_url)

def test_update_check_network_error():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError('Network error')

        with patch('migrated_module.requests.get') as mock_get:
            mock_get.side_effect = requests.RequestException('Network error')

            with pytest.raises(urllib.error.URLError):
                urllib.request.urlopen(config.update_url)

            with pytest.raises(requests.RequestException):
                requests.get(config.update_url)

def test_invalid_json_response():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = 'Invalid JSON'.encode('utf-8')
        mock_urlopen.return_value = mock_response

        with patch('migrated_module.requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.text = 'Invalid JSON'
            mock_get.return_value = mock_response

            with pytest.raises(json.JSONDecodeError):
                json.loads(urllib.request.urlopen(config.update_url).read().decode('utf-8'))

            with pytest.raises(json.JSONDecodeError):
                json.loads(requests.get(config.update_url).text)