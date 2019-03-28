import requests
from rest_framework import serializers

class WanikaniApiKeyValidatorV1(object):
    #TODO write a similar one of these for v2 validation

    def __init__(self):
        self.failure_message = "This API key appears to be invalid"

    def __call__(self, value):
        api_string = self.build_v1_user_information_api_string(value)
        r = requests.get(api_string)
        if r.status_code == 200:
            json_data = r.json()
            # WK Seems to often change what their failure state is, lets check instead for positive state.
            if "user_information" in json_data.keys():
                return value

        raise serializers.ValidationError(self.failure_message)

    def build_v1_user_information_api_string(self, api_key):
        return "https://www.wanikani.com/api/user/{}/user-information".format(api_key)
