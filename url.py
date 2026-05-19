def create_policy(self, policy_info):
        try:
            return self.client.post_json('devicepolicies/', policy_info)
        except urllib2.HTTPError, err:
            if err.code == 400:
                raise self.BadParams()
            elif err.code == 409:
                data = json.loads(err.read())
                if 'inherits_from' in data['conflicts']:
                    raise self.BadPolicy()
            raise