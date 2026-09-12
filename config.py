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
    os.getenv("MAX_MATCHES_PER_SITE", "25")
)


# ============================================================
# PARALLELISATION
# ============================================================
#
# Nombre de bookmakers scrapés EN MEME TEMPS (chacun dans son
# propre navigateur). Plus haut = plus rapide, mais plus de RAM/
# CPU utilisés sur le runner, et plus de connexions simultanées
# ouvertes chez le fournisseur de proxy (Webshare) — à réduire
# si ça déclenche des erreurs de connexion en plus grand nombre.
# ============================================================

MAX_PARALLEL_BOOKMAKERS = int(
    os.getenv("MAX_PARALLEL_BOOKMAKERS", "4")
)


# ============================================================
# BOOKMAKERS
# ============================================================

BOOKMAKERS = {

    "betwinner": {
        "url": "https://betwinner.cm/fr/line/football",
    },

    "melbet": {
        "url": "https://melbet-cm.com/en/line/football",
    },

    "megapari": {
        "url": "https://5572183mp.pro/en/line/football",
    },

    "1win": {
        "url": "https://1win.com/betting/prematch",
    },

    "winwin": {
        "url": "https://winwin.bet/en/line/football",
    },

    "1xbet": {
        "url": "https://1xbet.cm/fr/line/football",
    },

    "paripesa": {
        "url": "https://paripesa.cm/fr/line/football",
    },

    "africa-bizbet": {
        "url": "https://africa-bizbet.com/en/line/football",
    },
}


BOOKMAKERS_LIST = list(BOOKMAKERS.keys())







