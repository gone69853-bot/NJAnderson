import os


# ============================================================
# PROXY WEBSHARE
# ============================================================

PROXY_SERVER = os.getenv(
    "PROXY_SERVER",
    "http://p.webshare.io:80"
)

PROXY_USERNAME = os.getenv(
    "PROXY_USERNAME",
    "kecsytba-rotate"
)

PROXY_PASSWORD = os.getenv(
    "PROXY_PASSWORD",
    ""
)


def proxy_config():

    if not PROXY_PASSWORD:
        print("ATTENTION : PROXY_PASSWORD absent.")
        return None

    return {
        "server": PROXY_SERVER,
        "username": PROXY_USERNAME,
        "password": PROXY_PASSWORD,
    }


# ============================================================
# LIMITES POUR ECONOMISER LA BANDE PASSANTE
# ============================================================

MAX_MATCHES_PER_SITE = int(
    os.getenv("MAX_MATCHES_PER_SITE", "20")
)


# ============================================================
# BOOKMAKERS
# ============================================================

BOOKMAKERS = {

    "betwinner": {
        "url": "https://betwinner.cm/fr/line",
    },

    "melbet": {
        "url": "https://melbet-cm.com/en/line",
    },

    "megapari": {
        "url": "https://5572183mp.pro/en/line",
    },

    "1win": {
        "url": "https://1win.com/betting/prematch",
    },

    "winwin": {
        "url": "https://winwin.bet/en/line/football",
    },

    "1xbet": {
        "url": "https://1xbet.cm/fr/line",
    },

    "paripesa": {
        "url": "https://paripesa.cm/fr/line",
    },
}


BOOKMAKERS_LIST = list(BOOKMAKERS.keys())
