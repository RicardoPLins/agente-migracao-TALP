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

class TestConversationScraper:
    def test_generateRequestData_original(self):
        convID = 123
        offset = 0
        chunkSize = 100
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        userID = "userID"
        outDir = "outDir"

        original_scraper = ConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir)
        original_data = original_scraper.generateRequestData()

        parsed_original_data = urllib.parse.parse_qs(original_data.decode('utf-8'))

        migrated_scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)
        migrated_data = migrated_scraper.generateRequestData(offset, "", chunkSize)

        assert parsed_original_data == migrated_data

    def test_generateRequestData_migrated(self):
        convID = 123
        offset = 0
        chunkSize = 100
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        outDir = "outDir"

        migrated_scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)
        migrated_data = migrated_scraper.generateRequestData(offset, "", chunkSize)

        assert isinstance(migrated_data, dict)

    @responses.activate
    def test_executeRequest_migrated(self):
        convID = 123
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        outDir = "outDir"

        migrated_scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)

        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     json={"key": "value"}, status=200)

        migrated_data = migrated_scraper.executeRequest({"key": "value"})

        assert isinstance(migrated_data, str)

    @patch('urllib.request.urlopen')
    def test_executeRequest_original(self, mock_urlopen):
        convID = 123
        offset = 0
        chunkSize = 100
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        userID = "userID"
        outDir = "outDir"

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

        mock_urlopen.return_value = mock_resp

        original_scraper = ConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir)
        original_data = original_scraper.executeRequest(original_scraper.generateRequestData())

        assert isinstance(original_data, str)

    def test_init_original(self):
        convID = 123
        offset = 0
        chunkSize = 100
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        userID = "userID"
        outDir = "outDir"

        original_scraper = ConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir)

        assert original_scraper._directory == outDir + "/" + str(convID) + "/"
        assert original_scraper._convID == convID
        assert original_scraper._timestamp == ""
        assert original_scraper._offset == offset
        assert original_scraper._chunkSize == chunkSize
        assert original_scraper._cookie == cookie
        assert original_scraper._fb_dtsg == fb_dtsg
        assert original_scraper._userID == userID

    def test_init_migrated(self):
        convID = 123
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        outDir = "outDir"

        migrated_scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)

        assert migrated_scraper._directory == outDir + "/" + str(convID)
        assert migrated_scraper._convID == convID
        assert migrated_scraper._cookie == cookie
        assert migrated_scraper._fb_dtsg == fb_dtsg

    @patch('urllib.request.urlopen')
    def test_executeRequest_original_network_error(self, mock_urlopen):
        convID = 123
        offset = 0
        chunkSize = 100
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        userID = "userID"
        outDir = "outDir"

        mock_urlopen.side_effect = urllib.error.URLError("reason")

        original_scraper = ConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir)

        with pytest.raises(urllib.error.URLError):
            original_scraper.executeRequest(original_scraper.generateRequestData())

    @responses.activate
    def test_executeRequest_migrated_network_error(self):
        convID = 123
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        outDir = "outDir"

        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     body=requests.exceptions.ConnectionError())

        migrated_scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)

        with pytest.raises(requests.exceptions.ConnectionError):
            migrated_scraper.executeRequest({"key": "value"})

    @patch('urllib.request.urlopen')
    def test_executeRequest_original_http_error(self, mock_urlopen):
        convID = 123
        offset = 0
        chunkSize = 100
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        userID = "userID"
        outDir = "outDir"

        mock_urlopen.side_effect = urllib.error.HTTPError("url", 404, "msg", {}, None)

        original_scraper = ConversationScraper(convID, offset, chunkSize, cookie, fb_dtsg, userID, outDir)

        original_scraper.executeRequest(original_scraper.generateRequestData())

    @responses.activate
    def test_executeRequest_migrated_http_error(self):
        convID = 123
        cookie = "cookie"
        fb_dtsg = "fb_dtsg"
        outDir = "outDir"

        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     json={"key": "value"}, status=404)

        migrated_scraper = ConversationScraper(convID, cookie, fb_dtsg, outDir)

        response = migrated_scraper.executeRequest({"key": "value"})

        assert response.startswith("X" * 9)