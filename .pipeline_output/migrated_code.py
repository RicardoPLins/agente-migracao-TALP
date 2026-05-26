"""
Legacy requests-based HTTP client module.
This file intentionally contains multiple requests usages
for migration and refactoring practice.
"""

import json
import ssl
import time
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError, ConnectionError, Timeout, RequestException


BASE_URL = "https://api.example.com"
DEFAULT_TIMEOUT = 10


def create_ssl_context():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def build_headers(token=None):
    headers = {
        "User-Agent": "LegacyClient/1.0",
        "Accept": "application/json",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def encode_query(params):
    return requests.compat.urlencode(params)


def build_url(path, params=None):
    url = f"{BASE_URL}/{path}"

    if params:
        query = encode_query(params)
        url = f"{url}?{query}"

    return url


def fetch_users():
    url = build_url("users")

    response = requests.get(url, timeout=DEFAULT_TIMEOUT)

    return response.json()


def fetch_user_by_id(user_id):
    url = build_url(f"users/{user_id}")

    response = requests.get(url, headers=build_headers())

    return response.json()


def create_user(payload, token):
    url = build_url("users")

    data = json.dumps(payload)

    response = requests.post(url, data=data, headers=build_headers(token))

    return response.json()


def update_user(user_id, payload, token):
    url = build_url(f"users/{user_id}")

    data = json.dumps(payload)

    response = requests.put(url, data=data, headers=build_headers(token))

    return response.text


def delete_user(user_id, token):
    url = build_url(f"users/{user_id}")

    response = requests.delete(url, headers=build_headers(token))

    return response.status_code


def download_report(report_id, output_file):
    url = build_url(f"reports/{report_id}/download")

    response = requests.get(url)

    with open(output_file, "wb") as file:
        file.write(response.content)


def send_form_data(form):
    url = build_url("forms/submit")

    response = requests.post(url, data=form)

    return response.text


def upload_metrics(metrics):
    url = build_url("metrics")

    data = json.dumps(metrics)

    response = requests.post(url, data=data, headers={"Content-Type": "application/json"})

    return response.json()


def fetch_with_retry(path, retries=3):
    url = build_url(path)

    for attempt in range(retries):
        try:
            response = requests.get(url)

            return response.text

        except ConnectionError:
            print(f"Retry {attempt + 1}")
            time.sleep(1)

    return None


def configure_proxy():
    proxies = {
        "http": "http://proxy.local:8080",
        "https": "http://proxy.local:8080",
    }

    adapter = HTTPAdapter()
    session = requests.Session()
    session.proxies = proxies
    session.mount("http://", adapter)
    session.mount("https://", adapter)


def ping_service():
    url = build_url("health")

    response = requests.get(url)

    return response.status_code == 200


def fetch_binary_asset(asset_id):
    url = build_url(f"assets/{asset_id}")

    response = requests.get(url)

    return response.content


def fetch_headers():
    url = build_url("headers")

    response = requests.get(url)

    return dict(response.headers)


def submit_feedback(message, email):
    url = build_url("feedback")

    payload = {
        "message": message,
        "email": email,
    }

    data = json.dumps(payload)

    response = requests.post(url, data=data, headers={"Content-Type": "application/json"})

    return response.text


def fetch_secure_data():
    url = build_url("secure")

    response = requests.get(url, verify=False)

    return response.text


def execute_batch_requests(ids):
    results = []

    for item_id in ids:
        url = build_url(f"items/{item_id}")

        response = requests.get(url)

        results.append(response.json())

    return results


def main():
    configure_proxy()

    users = fetch_users()

    print("Users:", users)

    health = ping_service()

    print("Service healthy:", health)

    metrics = {
        "cpu": 45,
        "memory": 70,
    }

    upload_metrics(metrics)

    submit_feedback(
        message="System running correctly",
        email="admin@example.com"
    )


if __name__ == "__main__":
    main()