import json
import logging
import socket
import urllib.error
import urllib.request

from config import SUPABASE_KEY, SUPABASE_URL


class SupabaseClient:
    """Inserts rows into the Supabase events table via the REST API."""

    def __init__(self) -> None:
        self._endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/events"
        self._source = socket.gethostname()
        self._headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

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
            logging.warning("supabase %s error: %s", e.code, e.read(200))
        except Exception as e:
            logging.warning("supabase push failed: %s", e)
