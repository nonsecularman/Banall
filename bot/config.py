from os import getenv

class Config:
    TELEGRAM_TOKEN = getenv("TELEGRAM_TOKEN")
    PYRO_SESSION = getenv("PYRO_SESSION")  # optional
    TELEGRAM_APP_HASH = getenv("TELEGRAM_APP_HASH")
    TELEGRAM_APP_ID = getenv("TELEGRAM_APP_ID")

    # Required checks
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set")

    if not TELEGRAM_APP_HASH:
        raise ValueError("TELEGRAM_APP_HASH not set")

    if not TELEGRAM_APP_ID:
        raise ValueError("TELEGRAM_APP_ID not set")

    # Safe type casting
    TELEGRAM_APP_ID = int(TELEGRAM_APP_ID)
