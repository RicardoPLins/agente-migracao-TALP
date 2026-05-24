import pytest
import json
import gzip
import io
from unittest.mock import MagicMock, patch
from urllib.error import URLError, HTTPError
from requests.exceptions import ConnectionError, Timeout
import responses
import logging
import os
import sys
import time
import configparser
import argparse
from original_module import ConversationScraper as OriginalConversationScraper
from migrated_module import ConversationScraper as MigratedConversationScraper

# Mocking setup
PAYLOAD = {"key": "value"}
compressed = gzip.compress(json.dumps(PAYLOAD).encode())
buf = io.BytesIO(compressed)

mock_resp = MagicMock()
mock_resp.read.side_effect = buf.read
mock_resp.readable.return_value = True
mock_resp.seekable.return_value = False
mock_resp.writable.return_value = False
mock_resp.__enter__ = lambda s: s
mock_resp.__exit__ = MagicMock(return_value=False)

PREFIX = "X" * 9

# Test cases
def test_generateRequestData():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    original_data = original_scraper.generateRequestData()
    migrated_data = migrated_scraper.generateRequestData(0, "", 2000, False)

    original_parsed = dict(json.loads('{"' + original_data.decode('utf-8').replace('&', '","').replace('=', '":"') + '"}'))
    assert original_parsed == migrated_data

def test_executeRequest():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    with patch('urllib.request.urlopen', return_value=mock_resp):
        original_response = original_scraper.executeRequest(original_scraper.generateRequestData())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=json.dumps(PAYLOAD), status=200)
        migrated_response = migrated_scraper.executeRequest(migrated_scraper.generateRequestData(0, "", 2000, False))

    assert json.loads(original_response) == PAYLOAD
    assert json.loads(migrated_response) == PAYLOAD

def test_executeRequest_http_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    with patch('urllib.request.urlopen', side_effect=HTTPError("url", 404, "msg", {}, None)):
        with pytest.raises(HTTPError):
            original_scraper.executeRequest(original_scraper.generateRequestData())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 status=404)
        response = migrated_scraper.executeRequest(migrated_scraper.generateRequestData(0, "", 2000, False))
        with pytest.raises(json.JSONDecodeError):
            json.loads(response)

def test_executeRequest_network_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    with patch('urllib.request.urlopen', side_effect=URLError("reason")):
        with pytest.raises(URLError):
            original_scraper.executeRequest(original_scraper.generateRequestData())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=ConnectionError())
        with pytest.raises(ConnectionError):
            migrated_scraper.executeRequest(migrated_scraper.generateRequestData(0, "", 2000, False))

def test_scrapeConversation():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    with patch('original_module.ConversationScraper.executeRequest', return_value='{"payload": {"actions": []}}'):
        original_scraper.scrapeConversation(False)

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=json.dumps({"payload": {"actions": []}}), status=200)
        migrated_scraper.scrapeConversation(False, 0, 0, 2000, 0, False)

def test_scrapeConversation_http_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    with patch('original_module.ConversationScraper.executeRequest', side_effect=HTTPError("url", 404, "msg", {}, None)):
        with pytest.raises(HTTPError):
            original_scraper.scrapeConversation(False)

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 status=404)
        with pytest.raises(json.JSONDecodeError):
            migrated_scraper.scrapeConversation(False, 0, 0, 2000, 0, False)

def test_scrapeConversation_network_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    with patch('original_module.ConversationScraper.executeRequest', side_effect=URLError("reason")):
        with pytest.raises(URLError):
            original_scraper.scrapeConversation(False)

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=ConnectionError())
        with pytest.raises(ConnectionError):
            migrated_scraper.scrapeConversation(False, 0, 0, 2000, 0, False)