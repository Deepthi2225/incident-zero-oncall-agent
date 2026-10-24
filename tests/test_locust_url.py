"""Locust is reached through the demo's proxy at /loadgen (verified on the running demo)."""
import json
import urllib.request

from actionapi import config
from actionapi.service import LocustClient


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_default_url_is_the_proxy_path():
    source = (config.ROOT / "actionapi" / "config.py").read_text(encoding="utf-8")
    assert 'os.getenv("LOCUST_URL", "http://localhost:8080/loadgen")' in source
    assert "8089" not in source  # Locust's own port is not published to a fixed host port


def test_requests_go_to_loadgen_stats_and_swarm(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.data))
        return Resp({"user_count": 5, "state": "running"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LocustClient("http://localhost:8080/loadgen")
    assert client.users() == (5, "running")
    client.set_users(2)
    assert seen[0][:2] == ("GET", "http://localhost:8080/loadgen/stats/requests")
    assert seen[1][:2] == ("POST", "http://localhost:8080/loadgen/swarm")
    assert b"user_count=2" in seen[1][2]
