import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch
import pytest
from original_module import ConversationScraper as OriginalConversationScraper
from migrated_module import ConversationScraper as MigratedConversationScraper

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

def test_generate_request_data_equivalence():
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    payload = {"key": "value"}
    original_output = original_scraper.generateRequestData(payload)
    migrated_output = migrated_scraper.generateRequestData(payload)

    assert json.loads(urllib.parse.unquote(original_output.decode('utf-8'))) == migrated_output

def test_execute_request_happy_path():
    @responses.activate
    def test():
        responses.add(responses.GET, 'https://api.example.com/users',
                      body=json.dumps({"key": "value"}), status=200)

        original_scraper = OriginalConversationScraper()
        migrated_scraper = MigratedConversationScraper()

        original_response = original_scraper.executeRequest('https://api.example.com/users')
        migrated_response = migrated_scraper.executeRequest('https://api.example.com/users')

        assert json.loads(original_response.decode('utf-8')) == json.loads(migrated_response.decode('utf-8'))

def test_execute_request_http_error():
    @responses.activate
    def test():
        responses.add(responses.GET, 'https://api.example.com/users',
                      status=404)

        original_scraper = OriginalConversationScraper()
        migrated_scraper = MigratedConversationScraper()

        with pytest.raises(urllib.error.HTTPError):
            original_scraper.executeRequest('https://api.example.com/users')

        with pytest.raises(requests.exceptions.HTTPError):
            migrated_scraper.executeRequest('https://api.example.com/users')

def test_execute_request_network_error():
    @responses.activate
    def test():
        responses.add(responses.GET, 'https://api.example.com/users',
                      body=requests.exceptions.ConnectionError())

        original_scraper = OriginalConversationScraper()
        migrated_scraper = MigratedConversationScraper()

        with pytest.raises(urllib.error.URLError):
            original_scraper.executeRequest('https://api.example.com/users')

        with pytest.raises(requests.exceptions.ConnectionError):
            migrated_scraper.executeRequest('https://api.example.com/users')

def test_init_attribute_checks():
    original_scraper = OriginalConversationScraper()
    migrated_scraper = MigratedConversationScraper()

    assert hasattr(original_scraper, 'generateRequestData')
    assert hasattr(migrated_scraper, 'generateRequestData')

    assert hasattr(original_scraper, 'executeRequest')
    assert hasattr(migrated_scraper, 'executeRequest')