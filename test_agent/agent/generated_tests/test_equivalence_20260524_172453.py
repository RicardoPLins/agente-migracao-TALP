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

def test_generateRequestData_original():
    scraper = OriginalConversationScraper("convID", 0, 100, "cookie", "fb_dtsg", "userID", "outDir")
    data = scraper.generateRequestData()
    parsed_data = urllib.parse.parse_qs(data.decode('utf-8'))
    assert parsed_data["messages[user_ids][convID][offset]"] == ['0']
    assert parsed_data["messages[user_ids][convID][timestamp]"] == ['']
    assert parsed_data["messages[user_ids][convID][limit]"] == ['100']
    assert parsed_data["client"] == ['web_messenger']
    assert parsed_data["fb_dtsg"] == ['fb_dtsg']

def test_generateRequestData_migrated():
    scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
    data = scraper.generateRequestData(0, "", 100)
    assert data["messages[user_ids][convID][offset]"] == '0'
    assert data["messages[user_ids][convID][timestamp]"] == ''
    assert data["messages[user_ids][convID][limit]"] == '100'
    assert data["client"] == 'web_messenger'
    assert data["fb_dtsg"] == 'fb_dtsg'

def test_executeRequest_original():
    scraper = OriginalConversationScraper("convID", 0, 100, "cookie", "fb_dtsg", "userID", "outDir")
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
    with patch('urllib.request.urlopen', return_value=mock_resp):
        response = scraper.executeRequest(b'')
        assert response == json.dumps(PAYLOAD)

def test_executeRequest_migrated():
    scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 json={"key": "value"}, status=200)
        response = scraper.executeRequest({})
        assert response == '{"key": "value"}'

def test_executeRequest_original_network_error():
    scraper = OriginalConversationScraper("convID", 0, 100, "cookie", "fb_dtsg", "userID", "outDir")
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("reason")):
        with pytest.raises(urllib.error.URLError):
            scraper.executeRequest(b'')

def test_executeRequest_migrated_network_error():
    scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=requests.exceptions.ConnectionError())
        with pytest.raises(requests.exceptions.ConnectionError):
            scraper.executeRequest({})

def test_executeRequest_original_http_error():
    scraper = OriginalConversationScraper("convID", 0, 100, "cookie", "fb_dtsg", "userID", "outDir")
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
    mock_resp.getcode.return_value = 404
    with patch('urllib.request.urlopen', return_value=mock_resp):
        response = scraper.executeRequest(b'')
        assert response == json.dumps(PAYLOAD)

def test_executeRequest_migrated_http_error():
    scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 json={"key": "value"}, status=404)
        response = scraper.executeRequest({})
        assert response == '{"key": "value"}'

def test_init_original():
    scraper = OriginalConversationScraper("convID", 0, 100, "cookie", "fb_dtsg", "userID", "outDir")
    assert scraper._directory == "outDir/convID/"
    assert scraper._convID == "convID"
    assert scraper._timestamp == ""
    assert scraper._offset == 0
    assert scraper._chunkSize == 100
    assert scraper._cookie == "cookie"
    assert scraper._fb_dtsg == "fb_dtsg"
    assert scraper._userID == "userID"

def test_init_migrated():
    scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
    assert scraper._directory == "outDir/convID"
    assert scraper._convID == "convID"
    assert scraper._cookie == "cookie"
    assert scraper._fb_dtsg == "fb_dtsg"