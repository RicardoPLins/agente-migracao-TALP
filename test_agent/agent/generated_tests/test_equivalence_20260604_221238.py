import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch, Mock
import pytest
from original_module import _update as orig_update
from original_module import _check as orig_check
from migrated_module import _update as mig_update
from migrated_module import _check as mig_check

@pytest.mark.parametrize("func", [orig_update, orig_check, mig_update, mig_check])
def test_init(func):
    assert callable(func)

@responses.activate
def test_update_happy_path():
    url = 'http://nettacker.z3r0y.com/version.py'
    data = '1.0 test'
    responses.add(responses.GET, url, body=data, status=200)
    orig_update('1.0', 'test', 'en')
    mig_update('1.0', 'test', 'en')

@responses.activate
def test_check_happy_path():
    url = 'http://nettacker.z3r0y.com/version.py'
    data = '1.0 test'
    responses.add(responses.GET, url, body=data, status=200)
    orig_check('1.0', 'test', 'en')
    mig_check('1.0', 'test', 'en')

@responses.activate
def test_update_http_error():
    url = 'http://nettacker.z3r0y.com/version.py'
    responses.add(responses.GET, url, status=404)
    orig_update('1.0', 'test', 'en')
    mig_update('1.0', 'test', 'en')

@responses.activate
def test_check_http_error():
    url = 'http://nettacker.z3r0y.com/version.py'
    responses.add(responses.GET, url, status=404)
    orig_check('1.0', 'test', 'en')
    mig_check('1.0', 'test', 'en')

@responses.activate
def test_update_network_error():
    url = 'http://nettacker.z3r0y.com/version.py'
    responses.add(responses.GET, url, body=requests.exceptions.ConnectionError())
    orig_update('1.0', 'test', 'en')
    mig_update('1.0', 'test', 'en')

@responses.activate
def test_check_network_error():
    url = 'http://nettacker.z3r0y.com/version.py'
    responses.add(responses.GET, url, body=requests.exceptions.ConnectionError())
    orig_check('1.0', 'test', 'en')
    mig_check('1.0', 'test', 'en')

@responses.activate
def test_update_invalid_input():
    url = 'http://nettacker.z3r0y.com/version.py'
    data = 'invalid data'
    responses.add(responses.GET, url, body=data, status=200)
    orig_update('1.0', 'test', 'en')
    mig_update('1.0', 'test', 'en')

@responses.activate
def test_check_invalid_input():
    url = 'http://nettacker.z3r0y.com/version.py'
    data = 'invalid data'
    responses.add(responses.GET, url, body=data, status=200)
    orig_check('1.0', 'test', 'en')
    mig_check('1.0', 'test', 'en')

@patch('core.alert.info')
@patch('core.alert.warn')
@responses.activate
def test_update_response_parsing(mock_warn, mock_info):
    url = 'http://nettacker.z3r0y.com/version.py'
    data = '1.0 test'
    responses.add(responses.GET, url, body=data, status=200)
    orig_update('1.0', 'test', 'en')
    mig_update('1.0', 'test', 'en')
    mock_info.assert_called_once()
    mock_warn.assert_not_called()

@patch('core.alert.info')
@patch('core.alert.warn')
@responses.activate
def test_check_response_parsing(mock_warn, mock_info):
    url = 'http://nettacker.z3r0y.com/version.py'
    data = '1.0 test'
    responses.add(responses.GET, url, body=data, status=200)
    orig_check('1.0', 'test', 'en')
    mig_check('1.0', 'test', 'en')
    mock_info.assert_called_once()
    mock_warn.assert_not_called()