def create_user(self, user_info):
    try:
        response = requests.post(self.client.url + 'users/', json=user_info)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as err:
        if err.response.status_code == 400:
            raise self.BadParams()
        if err.response.status_code == 402:
            raise self.PaymentRequired()
        elif err.response.status_code == 409:
            data = err.response.json()
            if 'username' in data.get('conflicts', []):
                raise self.DuplicateUsername()
