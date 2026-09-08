import os
import json
import requests

# --- CONFIGURATION PROXY WEBSHARE ---
PROXY_HOST = "p.webshare.io"
PROXY_PORT = "80"
PROXY_USER = "hmbmocqu-JP-rotate"
PROXY_PASS = "3wba4sf52b64"

PROXY_URL = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"

PROXIES = {
    "http": PROXY_URL,
    "https": PROXY_URL
}

# --- LISTE COMPLÈTE DES 7 BOOKMAKERS ---
BOOKMAKERS_ENDPOINTS = {
    "1xbet": "https://1xbet.com/LineFeed/GetGamesZip",
    "betwinner": "https://betwinner.com/LineFeed/GetGamesZip",
    "melbet": "https://melbet.com/LineFeed/GetGamesZip",
    "winwin": "https://winwin.bet/LineFeed/GetGamesZip",
    "1win": "https://1win.pro/api/v2/matches",
    "megapari": "https://5572183mp.pro/LineFeed/GetGamesZip",
    "paripesa": "https://paripesa.cm/LineFeed/GetGamesZip"
}

session = requests.Session()
session.proxies.update(PROXIES)
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
})

def fetch_bookmaker_data(name, url):
    """
    Récupère les cotes en envoyant uniquement des requêtes compressées.
    """
    try:
        # Paramètres minimaux pour extraire le football / grands championnats
        params = {"sport": 1, "count": 20, "lng": "fr"} if "1win" not in name else {}
        response = session.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"Erreur [{name}]: {e}")
    return None

def process_all_odds():
    all_data = {}
    
    for bk_name, endpoint in BOOKMAKERS_ENDPOINTS.items():
        raw_json = fetch_bookmaker_data(bk_name, endpoint)
        if raw_json:
            all_data[bk_name] = raw_json

    # Sauvegarde JSON minifiée (sans espaces inutilement lourds)
    os.makedirs("docs", exist_ok=True)
    with open("docs/odds_data.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, separators=(',', ':'))

if __name__ == "__main__":
    process_all_odds()

