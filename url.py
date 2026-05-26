"""
Legacy urllib-based HTTP client module.
This file intentionally contains multiple urllib usages
for migration and refactoring practice.
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error
from urllib.request import (
    Request,
    urlopen,
    build_opener,
    install_opener,
    ProxyHandler,
)
from urllib.parse import urlencode


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
    return urlencode(params)


def build_url(path, params=None):
    url = f"{BASE_URL}/{path}"

    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"

    return url


def fetch_users():
    url = build_url("users")

    request = Request(url)

    response = urlopen(request, timeout=DEFAULT_TIMEOUT)

    body = response.read().decode("utf-8")

    return json.loads(body)


def fetch_user_by_id(user_id):
    url = build_url(f"users/{user_id}")

    request = Request(
        url,
        headers=build_headers()
    )

    response = urllib.request.urlopen(request)

    data = response.read().decode("utf-8")

    return json.loads(data)


def create_user(payload, token):
    url = build_url("users")

    data = json.dumps(payload).encode("utf-8")

    request = Request(
        url,
        data=data,
        headers=build_headers(token),
        method="POST"
    )

    response = urlopen(request)

    content = response.read().decode("utf-8")

    return json.loads(content)


def update_user(user_id, payload, token):
    url = build_url(f"users/{user_id}")

    body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers=build_headers(token),
        method="PUT"
    )

    response = urllib.request.urlopen(request)

    return response.read().decode("utf-8")


def delete_user(user_id, token):
    url = build_url(f"users/{user_id}")

    request = Request(
        url,
        headers=build_headers(token),
        method="DELETE"
    )

    response = urlopen(request)

    return response.getcode()


def download_report(report_id, output_file):
    url = build_url(f"reports/{report_id}/download")

    request = Request(url)

    response = urllib.request.urlopen(request)

    with open(output_file, "wb") as file:
        file.write(response.read())


def send_form_data(form):
    url = build_url("forms/submit")

    encoded_data = urllib.parse.urlencode(form).encode("utf-8")

    request = Request(
        url,
        data=encoded_data
    )

    response = urlopen(request)

    return response.read().decode("utf-8")


def upload_metrics(metrics):
    url = build_url("metrics")

    payload = json.dumps(metrics).encode("utf-8")

    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json"
        }
    )

    try:
        response = urllib.request.urlopen(request)

        body = response.read().decode("utf-8")

        return json.loads(body)

    except urllib.error.HTTPError as error:
        print("HTTP Error:", error.code)
        return None

    except urllib.error.URLError as error:
        print("URL Error:", error.reason)
        return None


def fetch_with_retry(path, retries=3):
    url = build_url(path)

    for attempt in range(retries):
        try:
            request = Request(url)

            response = urlopen(request)

            return response.read().decode("utf-8")

        except urllib.error.URLError:
            print(f"Retry {attempt + 1}")
            time.sleep(1)

    return None


def configure_proxy():
    proxy = ProxyHandler({
        "http": "http://proxy.local:8080",
        "https": "http://proxy.local:8080",
    })

    opener = build_opener(proxy)

    install_opener(opener)


def ping_service():
    url = build_url("health")

    request = Request(url)

    response = urlopen(request)

    return response.status == 200


def fetch_binary_asset(asset_id):
    url = build_url(f"assets/{asset_id}")

    request = Request(url)

    response = urllib.request.urlopen(request)

    return response.read()


def fetch_headers():
    url = build_url("headers")

    request = Request(url)

    response = urlopen(request)

    return dict(response.headers)


def submit_feedback(message, email):
    url = build_url("feedback")

    payload = {
        "message": message,
        "email": email,
    }

    data = json.dumps(payload).encode("utf-8")

    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    response = urllib.request.urlopen(request)

    return response.read().decode("utf-8")


def fetch_secure_data():
    url = build_url("secure")

    context = create_ssl_context()

    request = Request(url)

    response = urllib.request.urlopen(
        request,
        context=context
    )

    return response.read().decode("utf-8")


def execute_batch_requests(ids):
    results = []

    for item_id in ids:
        url = build_url(f"items/{item_id}")

        request = Request(url)

        response = urlopen(request)

        body = response.read().decode("utf-8")

        results.append(json.loads(body))

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