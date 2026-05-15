 try:
            resp = self.client.post_json_raw_response(
                'groups/', group_info)
        except urllib2.HTTPError, err:
            if err.code == 400:
                raise self.BadParams()
            elif err.code == 409:
                data = json.loads(err.read())
                if 'name' in data['conflicts']:
                    raise self.DuplicateGroupName()
                elif 'plan_id' in data['conflicts']:
                    raise self.BadPlan()
            raise