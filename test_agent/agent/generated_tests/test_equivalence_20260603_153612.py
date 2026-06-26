import pytest
import json
from unittest.mock import patch, MagicMock
import urllib.request
import requests

# Assuming config module with update_url
import config

def test_update_check_happy_path_original():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([{'tag_name': 'new_version'}]).encode('utf-8')
        mock_urlopen.return_value = mock_response

        urllib.request.urlopen(config.update_url)
        mock_urlopen.assert_called_once_with(config.update_url)

def test_update_check_happy_path_migrated():
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = json.dumps([{'tag_name': 'new_version'}])
        mock_get.return_value = mock_response

        requests.get(config.update_url)
        mock_get.assert_called_once_with(config.update_url)

def test_update_check_http_error_original():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError('url', 404, 'msg', {}, None)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(config.update_url)

def test_update_check_http_error_migrated():
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response
        if config.MODULE_QUIRKS['migrated']['raises_on_http_error']:
            with pytest.raises(requests.RequestException):
                requests.get(config.update_url)
        else:
            requests.get(config.update_url)

def test_update_check_network_error_original():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError('reason')
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(config.update_url)

def test_update_check_network_error_migrated():
    with patch('requests.get') as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError()
        with pytest.raises(requests.RequestException):
            requests.get(config.update_url)

def test_update_check_response_parsing_original():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([{'tag_name': 'new_version'}]).encode('utf-8')
        mock_urlopen.return_value = mock_response

        response = urllib.request.urlopen(config.update_url)
        data = json.loads(response.read().decode('utf-8'))
        assert data[0]['tag_name'] == 'new_version'

def test_update_check_response_parsing_migrated():
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = json.dumps([{'tag_name': 'new_version'}])
        mock_get.return_value = mock_response

        response = requests.get(config.update_url)
        data = json.loads(response.text)
        assert data[0]['tag_name'] == 'new_version'