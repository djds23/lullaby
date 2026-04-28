import json
import logging
import socket
import urllib.error
import urllib.request

from config import POCKETBASE_TOKEN, POCKETBASE_URL


class PocketBaseClient:
    """Inserts rows into a PocketBase collection via the REST API."""

    def __init__(self, collection: str = "events") -> None:
        self._endpoint = f"{POCKETBASE_URL.rstrip('/')}/api/collections/{collection}/records"
        self._source = socket.gethostname()
        self._headers = {"Content-Type": "application/json"}
        if POCKETBASE_TOKEN:
            self._headers["Authorization"] = POCKETBASE_TOKEN

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
            logging.warning("pocketbase %s error: %s", e.code, e.read(200))
        except Exception as e:
            logging.warning("pocketbase push failed: %s", e)
