#!/usr/bin/env python
# -*- coding: utf-8 -*-

from core.alert import info
from core.alert import warn
from core.alert import messages
from core.compatible import version

url = 'http://nettacker.z3r0d4y.com/version.py'


def _update(__version__, __code_name__, language):
    try:
        response = requests.get(url, timeout=5)
        data = response.text
        if response.history:  # Check for redirects
            warn(messages(language, 104))  # Assuming a new message code for redirect warning
        if __version__ + ' ' + __code_name__ == data.rsplit('\n')[0]:
            info(messages(language, 103))
        else:
            warn(messages(language, 101))
            warn(messages(language, 85))
    except requests.exceptions.RequestException as e:
        warn(messages(language, 102) + str(e))  # Include the exception message for debugging
    return


def _check(__version__, __code_name__, language):
    try:
        response = requests.get(url, timeout=5)
        data = response.text
        if response.history:  # Check for redirects
            warn(messages(language, 104))  # Assuming a new message code for redirect warning
        if __version__ + ' ' + __code_name__ == data.rsplit('\n')[0]:
            info(messages(language, 103))
        else:
            warn(messages(language, 101))
    except requests.exceptions.RequestException as e:
        warn(messages(language, 102) + str(e))  # Include the exception message for debugging
    return