import os


def load_env(path: str = "/home/deanrex/.env") -> None:
    """Minimal .env loader — no dependencies."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


load_env()

POCKETBASE_URL   = os.environ.get("POCKETBASE_URL", "http://tenor.local:8090")
POCKETBASE_TOKEN = os.environ.get("POCKETBASE_TOKEN", "")
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", "300"))
