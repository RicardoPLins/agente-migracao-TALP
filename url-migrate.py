def _do_query(self, method, parameters={}):
    parameters_str = urllib.parse.urlencode(parameters)
    url = ''.join([
        toutv.config.TOUTV_JSON_URL,
        method,
        '?',
        parameters_str])
    headers = {'User-Agent': toutv.config.USER_AGENT}
    response = requests.get(url, headers=headers)
    json_string = response.text
    json_decoded = self.json_decoder.decode(json_string)
    return json_decoded['d']
