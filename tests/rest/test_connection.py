import pytest
import requests_mock

from openeo.rest.auth.auth import NullAuth, BearerAuth
from openeo.rest.connection import Connection, RestApiConnection

API_URL = "https://oeo.net/"


@pytest.mark.parametrize(
    ["base", "paths", "expected_path"],
    [
        # Simple
        ("https://oeo.net", ["foo", "/foo"], "https://oeo.net/foo"),
        ("https://oeo.net/", ["foo", "/foo"], "https://oeo.net/foo"),
        # With trailing slash
        ("https://oeo.net", ["foo/", "/foo/"], "https://oeo.net/foo/"),
        ("https://oeo.net/", ["foo/", "/foo/"], "https://oeo.net/foo/"),
        # Deeper
        ("https://oeo.net/api/v04", ["foo/bar", "/foo/bar"], "https://oeo.net/api/v04/foo/bar"),
        ("https://oeo.net/api/v04/", ["foo/bar", "/foo/bar"], "https://oeo.net/api/v04/foo/bar"),
        ("https://oeo.net/api/v04", ["foo/bar/", "/foo/bar/"], "https://oeo.net/api/v04/foo/bar/"),
        ("https://oeo.net/api/v04/", ["foo/bar/", "/foo/bar/"], "https://oeo.net/api/v04/foo/bar/"),
    ]
)
def test_rest_api_connection_url_handling(requests_mock, base, paths, expected_path):
    """Test connection __init__ and proper joining of root url and API path"""
    conn = RestApiConnection(base)
    requests_mock.get(expected_path, text="payload")
    requests_mock.post(expected_path, text="payload")
    for path in paths:
        assert conn.get(path).text == "payload"
        assert conn.post(path, {"foo": "bar"}).text == "payload"


def test_rest_api_headers():
    conn = RestApiConnection(API_URL)
    with requests_mock.Mocker() as m:
        def text(request, context):
            assert request.headers["User-Agent"].startswith("openeo-python-client")
            assert request.headers["X-Openeo-Bar"] == "XY123"

        m.get("/foo", text=text)
        m.post("/foo", text=text)
        conn.get("/foo", headers={"X-Openeo-Bar": "XY123"})
        conn.post("/foo", {}, headers={"X-Openeo-Bar": "XY123"})


def test_authenticate_basic(requests_mock):
    conn = Connection(API_URL)

    def text_callback(request, context):
        assert request.headers["Authorization"] == "Basic am9objpqMGhu"
        return '{"access_token":"w3lc0m3"}'

    requests_mock.get('https://oeo.net/credentials/basic', text=text_callback)

    assert isinstance(conn.auth, NullAuth)
    conn.authenticate_basic(username="john", password="j0hn")
    assert isinstance(conn.auth, BearerAuth)
    assert conn.auth.bearer == "w3lc0m3"


def test_authenticate_oidc(oidc_test_setup):
    # see test/rest/conftest.py for `oidc_test_setup` fixture
    client_id = "myclient"
    oidc_discovery_url = "https://oeo.net/credentials/oidc"
    state, webbrowser_open = oidc_test_setup(client_id=client_id, oidc_discovery_url=oidc_discovery_url)

    # With all this set up, kick off the openid connect flow
    conn = Connection(API_URL)
    assert isinstance(conn.auth, NullAuth)
    conn.authenticate_OIDC(client_id=client_id, webbrowser_open=webbrowser_open)
    assert isinstance(conn.auth, BearerAuth)
    assert conn.auth.bearer == state["access_token"]
