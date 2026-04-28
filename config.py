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

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
