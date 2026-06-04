#!/usr/bin/env python
# -*- coding: utf-8 -*-

from core.alert import info
from core.alert import warn
from core.alert import messages
from core.compatible import version

url = 'http://nettacker.z3r0d4y.com/version.py'


def _update(__version__, __code_name__, language):
    from core.compatible import version
    if version() is 2:
        pass  # No need to handle Python 2 in requests
    if version() is 3:
        response = requests.get(url)
        data = response.text
        if __version__ + ' ' + __code_name__ == data.rsplit('\n')[0]:
            info(messages(language, 103))
        else:
            warn(messages(language, 101))
            warn(messages(language, 85))
    return


def _check(__version__, __code_name__, language):
    from core.compatible import version
    if version() is 2:
        pass  # No need to handle Python 2 in requests
    if version() is 3:
        response = requests.get(url)
        data = response.text
        if __version__ + ' ' + __code_name__ == data.rsplit('\n')[0]:
            info(messages(language, 103))
        else:
            warn(messages(language, 101))
    return
