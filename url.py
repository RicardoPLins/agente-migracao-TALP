 def create_user(self, user_info):
        try:
            return self.client.post_json('users/', user_info)
        except urllib2.HTTPError, err:
            if err.code == 400:
                raise self.BadParams()
            if err.code == 402:
                raise self.PaymentRequired()
            elif err.code == 409:
                data = json.loads(err.read())
                if 'username' in data['conflicts']:
                    raise self.DuplicateUsername()