import pytest
import json
from unittest.mock import MagicMock, patch
from original_module import ConversationScraper as OriginalClass
from migrated_module import ConversationScraper as MigratedClass
import responses
import requests
import gzip
import io

@pytest.fixture
def setup_module_quirks():
    return {
        "original": {
            "uses_gzip": True,
            "response_strip_chars": 9,
            "raises_on_http_error": False,
            "raises_on_network_error": False,
            "generateRequestData_return_type": "bytes",
            "local_imports": [],
            "missing_imports": [],
            "module_style": "class",
            "main_class_name": "ConversationScraper"
        },
        "migrated": {
            "uses_gzip": True,
            "response_strip_chars": 0,
            "raises_on_http_error": True,
            "raises_on_network_error": False,
            "generateRequestData_return_type": "dict",
            "local_imports": [],
            "missing_imports": [],
            "module_style": "functions",
            "main_class_name": None
        }
    }

def test_generateRequestData(setup_module_quirks):
    original_scraper = OriginalClass("convID", "cookie", "fb_dtsg", "outDir")
    migrated_scraper = MigratedClass("convID", "cookie", "fb_dtsg", "outDir")
    
    offset = 0
    timestamp = ""
    chunkSize = 2000
    
    original_data = original_scraper.generateRequestData(offset, timestamp, chunkSize)
    migrated_data = migrated_scraper.generateRequestData(offset, timestamp, chunkSize)
    
    # Normalize for comparison
    original_data = urllib.parse.parse_qs(original_data.decode('utf-8'))
    migrated_data = migrated_data
    
    assert original_data == migrated_data

@responses.activate
def test_executeRequest(setup_module_quirks):
    original_scraper = OriginalClass("convID", "cookie", "fb_dtsg", "outDir")
    migrated_scraper = MigratedClass("convID", "cookie", "fb_dtsg", "outDir")
    
    offset = 0
    timestamp = ""
    chunkSize = 2000
    
    original_data = original_scraper.generateRequestData(offset, timestamp, chunkSize)
    migrated_data = migrated_scraper.generateRequestData(offset, timestamp, chunkSize)
    
    payload = {"key": "value"}
    compressed = gzip.compress(json.dumps(payload).encode())
    buf = io.BytesIO(compressed)
    mock_resp = MagicMock()
    mock_resp.read.side_effect = buf.read
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    
    with patch.object(original_scraper, 'executeRequest', return_value="X" * setup_module_quirks["original"]["response_strip_chars"] + json.dumps(payload)):
        original_response = original_scraper.executeRequest(original_data)
    
    responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body="X" * setup_module_quirks["migrated"]["response_strip_chars"] + json.dumps(payload), status=200)
    
    migrated_response = migrated_scraper.executeRequest(migrated_data)
    
    assert json.loads(original_response) == json.loads(migrated_response)

def test_init(setup_module_quirks):
    original_scraper = OriginalClass("convID", "cookie", "fb_dtsg", "outDir")
    migrated_scraper = MigratedClass("convID", "cookie", "fb_dtsg", "outDir")
    
    assert original_scraper._directory == migrated_scraper._directory
    assert original_scraper._convID == migrated_scraper._convID
    assert original_scraper._cookie == migrated_scraper._cookie
    assert original_scraper._fb_dtsg == migrated_scraper._fb_dtsg