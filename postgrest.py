import json
import logging
import socket
import urllib.error
import urllib.request

from config import POSTGREST_TOKEN, POSTGREST_URL


class PostgRESTClient:
    """Inserts rows into a PostgREST-backed table via the REST API."""

    def __init__(self, table: str = "events") -> None:
        self._endpoint = f"{POSTGREST_URL.rstrip('/')}/{table}"
        self._source = socket.gethostname()
        self._headers = {
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        if POSTGREST_TOKEN:
            self._headers["Authorization"] = f"Bearer {POSTGREST_TOKEN}"

    def push_event(self, event_type: str, payload: dict) -> None:
        body = json.dumps({"name": event_type, "source": self._source, "payload": payload}).encode()
        req = urllib.request.Request(
            self._endpoint, data=body, headers=self._headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
            logging.info("pushed %s", event_type)
        except urllib.error.HTTPError as e:
            logging.warning("postgrest %s error: %s", e.code, e.read(200))
        except Exception as e:
            logging.warning("postgrest push failed: %s", e)
