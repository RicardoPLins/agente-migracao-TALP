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
    "uses_gzip": True,
    "response_strip_chars": 9,
    "raises_on_http_error": False,
    "raises_on_network_error": False,
    "generateRequestData_return_type": "bytes",
    "local_imports": [],
    "missing_imports": []
  },
  "migrated": {
    "uses_gzip": False,
    "response_strip_chars": 9,
    "raises_on_http_error": True,
    "raises_on_network_error": True,
    "generateRequestData_return_type": "dict",
    "local_imports": [
      "logger"
    ],
    "missing_imports": [
      "util"
    ]
  },
  "behavioral_diffs": []
}

def generate_request_data_original(scraper):
    dataForm = {"messages[user_ids][" + str(scraper._convID) + "][offset]": str(scraper._offset),
                "messages[user_ids][" + str(scraper._convID) + "][timestamp]": scraper._timestamp,
                "messages[user_ids][" + str(scraper._convID) + "][limit]": str(scraper._chunkSize),
                "client": "web_messenger",
                "__a": "",
                "__dyn": "",
                "__req": "",
                "fb_dtsg": scraper._fb_dtsg}
    return urllib.parse.urlencode(dataForm).encode('utf-8')

def generate_request_data_migrated(scraper, offset, timestamp, chunkSize, isGroupConversation=False):
    ids_type = "thread_fbids" if isGroupConversation else "user_ids"
    dataForm = {"messages[{}][{}][offset]".format(ids_type, scraper._convID) : str(offset),
                "messages[{}][{}][timestamp]".format(ids_type, scraper._convID): timestamp,
                "messages[{}][{}][limit]".format(ids_type, scraper._convID): str(chunkSize),
                 "client": "web_messenger",
                 "__a": "",
                 "__dyn": "",
                 "__req": "",
                 "fb_dtsg": scraper._fb_dtsg}
    return dataForm

def test_generate_request_data():
    convID = 123
    offset = 0
    chunkSize = 100
    timestamp = 1643723400
    cookie = "some_cookie"
    fb_dtsg = "some_fb_dtsg"
    outDir = "/tmp"

    original_scraper = OriginalConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, 123, outDir)
    migrated_scraper = MigratedConversationScraper(convID, cookie, fb_dtsg, outDir)

    original_data = generate_request_data_original(original_scraper)
    migrated_data = generate_request_data_migrated(migrated_scraper, offset, timestamp, chunkSize)

    assert urllib.parse.parse_qs(original_data.decode('utf-8')) == urllib.parse.parse_qs(json.dumps(migrated_data).encode('utf-8'))

def test_execute_request():
    convID = 123
    cookie = "some_cookie"
    fb_dtsg = "some_fb_dtsg"
    outDir = "/tmp"

    original_scraper = OriginalConversationScraper(convID, 0, 100, cookie, fb_dtsg, 123, outDir)
    migrated_scraper = MigratedConversationScraper(convID, cookie, fb_dtsg, outDir)

    payload = {"key": "value"}
    compressed = gzip.compress(json.dumps(payload).encode())
    buf = io.BytesIO(compressed)

    @responses.activate
    def test_original():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                      body=buf.read, status=200)
        request_data = generate_request_data_original(original_scraper)
        response = original_scraper.executeRequest(request_data)
        assert json.loads(response) == payload

    @responses.activate
    def test_migrated():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                      body=json.dumps(payload), status=200)
        request_data = generate_request_data_migrated(migrated_scraper, 0, 0, 100)
        response = migrated_scraper.executeRequest(request_data)
        assert json.loads(response) == payload

def test_execute_request_http_error():
    convID = 123
    cookie = "some_cookie"
    fb_dtsg = "some_fb_dtsg"
    outDir = "/tmp"

    original_scraper = OriginalConversationScraper(convID, 0, 100, cookie, fb_dtsg, 123, outDir)
    migrated_scraper = MigratedConversationScraper(convID, cookie, fb_dtsg, outDir)

    @responses.activate
    def test_original():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                      status=404)
        request_data = generate_request_data_original(original_scraper)
        with pytest.raises(urllib.error.HTTPError):
            original_scraper.executeRequest(request_data)

    @responses.activate
    def test_migrated():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                      status=404)
        request_data = generate_request_data_migrated(migrated_scraper, 0, 0, 100)
        with pytest.raises(requests.exceptions.HTTPError):
            migrated_scraper.executeRequest(request_data)

def test_execute_request_network_error():
    convID = 123
    cookie = "some_cookie"
    fb_dtsg = "some_fb_dtsg"
    outDir = "/tmp"

    original_scraper = OriginalConversationScraper(convID, 0, 100, cookie, fb_dtsg, 123, outDir)
    migrated_scraper = MigratedConversationScraper(convID, cookie, fb_dtsg, outDir)

    @responses.activate
    def test_original():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                      body=requests.exceptions.ConnectionError())
        request_data = generate_request_data_original(original_scraper)
        with pytest.raises(urllib.error.URLError):
            original_scraper.executeRequest(request_data)

    @responses.activate
    def test_migrated():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                      body=requests.exceptions.ConnectionError())
        request_data = generate_request_data_migrated(migrated_scraper, 0, 0, 100)
        with pytest.raises(requests.exceptions.ConnectionError):
            migrated_scraper.executeRequest(request_data)

def test_init():
    convID = 123
    cookie = "some_cookie"
    fb_dtsg = "some_fb_dtsg"
    outDir = "/tmp"

    original_scraper = OriginalConversationScraper(convID, 0, 100, cookie, fb_dtsg, 123, outDir)
    migrated_scraper = MigratedConversationScraper(convID, cookie, fb_dtsg, outDir)

    assert original_scraper._convID == convID
    assert migrated_scraper._convID == convID