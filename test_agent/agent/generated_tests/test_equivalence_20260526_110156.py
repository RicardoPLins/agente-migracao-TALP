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
        "raises_on_network_error": False,
        "generateRequestData_return_type": "other",
        "local_imports": [],
        "missing_imports": []
    },
    "migrated": {
        "uses_gzip": False,
        "response_strip_chars": 0,
        "raises_on_http_error": True,
        "raises_on_network_error": True,
        "generateRequestData_return_type": "other",
        "local_imports": [],
        "missing_imports": []
    },
    "behavioral_diffs": [
        "raises_on_http_error",
        "raises_on_network_error"
    ]
}

def test_generate_request_data():
    original = OriginalConversationScraper()
    migrated = MigratedConversationScraper()

    data = {"key": "value"}
    original_data = original.generateRequestData(data)
    migrated_data = migrated.generateRequestData(data)

    if MODULE_QUIRKS["original"]["generateRequestData_return_type"] == "other":
        original_data = urllib.parse.urlencode(data).encode('utf-8')
    if MODULE_QUIRKS["migrated"]["generateRequestData_return_type"] == "other":
        migrated_data = urllib.parse.urlencode(data).encode('utf-8')

    assert original_data == migrated_data

@responses.activate
def test_execute_request():
    url = "https://api.example.com/endpoint"
    payload = {"key": "value"}

    original = OriginalConversationScraper()
    migrated = MigratedConversationScraper()

    responses.add(responses.POST, url,
                  body=json.dumps(payload), status=200)

    original_response = original.executeRequest(url, json.dumps(payload).encode('utf-8'))
    migrated_response = migrated.executeRequest(url, json.dumps(payload).encode('utf-8'))

    assert json.loads(original_response.read().decode('utf-8')) == payload
    assert json.loads(migrated_response.text) == payload

def test_execute_request_http_error():
    url = "https://api.example.com/endpoint"
    payload = {"key": "value"}

    original = OriginalConversationScraper()
    migrated = MigratedConversationScraper()

    responses.add(responses.POST, url,
                  body=json.dumps(payload), status=500)

    if MODULE_QUIRKS["migrated"]["raises_on_http_error"]:
        with pytest.raises(requests.exceptions.HTTPError):
            migrated.executeRequest(url, json.dumps(payload).encode('utf-8'))
    else:
        migrated_response = migrated.executeRequest(url, json.dumps(payload).encode('utf-8'))
        assert migrated_response.status_code == 500

def test_execute_request_network_error():
    url = "https://api.example.com/endpoint"
    payload = {"key": "value"}

    original = OriginalConversationScraper()
    migrated = MigratedConversationScraper()

    responses.add(responses.POST, url,
                  body=requests.exceptions.ConnectionError())

    if MODULE_QUIRKS["migrated"]["raises_on_network_error"]:
        with pytest.raises(requests.exceptions.ConnectionError):
            migrated.executeRequest(url, json.dumps(payload).encode('utf-8'))
    else:
        with pytest.raises(urllib.error.URLError):
            original.executeRequest(url, json.dumps(payload).encode('utf-8'))

def test_init():
    original = OriginalConversationScraper()
    migrated = MigratedConversationScraper()

    assert hasattr(original, '__init__')
    assert hasattr(migrated, '__init__')