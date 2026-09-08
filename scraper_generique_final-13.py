import os
import json
import time
import requests

# ==============================================================================
# CONFIGURATION PROXY WEBSHARE (ROTATION JP)
# ==============================================================================
PROXY_HOST = "p.webshare.io"
PROXY_PORT = "80"
PROXY_USER = "hmbmocqu-JP-rotate"
PROXY_PASS = "3wba4sf52b64"

PROXY_URL = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"

PROXIES = {
    "http": PROXY_URL,
    "https": PROXY_URL
}

# ==============================================================================
# CONFIGURATION DES 7 BOOKMAKERS
# ==============================================================================
BOOKMAKERS_CONFIG = {
    "1xbet": {
        "url": "https://1xbet.com/LineFeed/GetGamesZip",
        "type": "betb2b"
    },
    "betwinner": {
        "url": "https://betwinner.com/LineFeed/GetGamesZip",
        "type": "betb2b"
    },
    "melbet": {
        "url": "https://melbet.com/LineFeed/GetGamesZip",
        "type": "betb2b"
    },
    "winwin": {
        "url": "https://winwin.bet/LineFeed/GetGamesZip",
        "type": "betb2b"
    },
    "megapari": {
        "url": "https://5572183mp.pro/LineFeed/GetGamesZip",
        "type": "betb2b"
    },
    "paripesa": {
        "url": "https://paripesa.cm/LineFeed/GetGamesZip",
        "type": "betb2b"
    },
    "1win": {
        "url": "https://1win.pro/api/v2/matches",
        "type": "1win_custom"
    }
}

# ==============================================================================
# INITIALISATION DE LA SESSION (OPTIMISATION BANDE PASSANTE)
# ==============================================================================
session = requests.Session()
session.proxies.update(PROXIES)

# Compression GZIP/Deflate/BR stricte pour économiser ~80% de bande passante
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
})

def fetch_raw_data(name, config):
    """
    Exécute la requête HTTP de manière optimisée.
    """
    url = config["url"]
    
    # Paramètres de filtrage légers (Football uniquement, 20 matchs max)
    params = {
        "sport": 1,      # 1 = Football (évite de charger le tennis, basket, etc.)
        "count": 20,     # Limite le nombre d'événements par cycle
        "lng": "fr"
    } if config["type"] == "betb2b" else {"limit": 20, "sportId": 1}

    try:
        response = session.get(url, params=params, timeout=8)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"[ERR] Échec du scraping pour {name}: {e}")
    return None

# ==============================================================================
# PARSERS ULTRA-LÉGERS (STRIPPING DES DONNÉES)
# ==============================================================================
def parse_betb2b(data):
    """
    Parser universel pour 1xBet, Betwinner, Melbet, Winwin, MegaPari et PariPesa.
    Ne conserve que : Equipes, Cote 1, Cote X, Cote 2.
    """
    clean_matches = []
    games = data.get("Value", []) if isinstance(data, dict) else []
    
    for game in games:
        try:
            home = game.get("O1", "Inconnu")
            away = game.get("O2", "Inconnu")
            game_id = game.get("I")
            
            # Extraction des cotes 1x2 (Group 1 / SubGroup 1, 2, 3)
            odds = {"1": None, "X": None, "2": None}
            events = game.get("E", [])
            for ev in events:
                t = ev.get("T")
                if t == 1:
                    odds["1"] = ev.get("C")
                elif t == 2:
                    odds["X"] = ev.get("C")
                elif t == 3:
                    odds["2"] = ev.get("C")
            
            clean_matches.append({
                "id": game_id,
                "match": f"{home} vs {away}",
                "odds": odds
            })
        except Exception:
            continue
            
    return clean_matches

def parse_1win(data):
    """
    Parser spécifique pour 1Win.
    """
    clean_matches = []
    matches = data.get("data", []) if isinstance(data, dict) else []
    
    for m in matches:
        try:
            home = m.get("homeTeam", {}).get("name", "Inconnu")
            away = m.get("awayTeam", {}).get("name", "Inconnu")
            game_id = m.get("id")
            
            odds_data = m.get("odds", {})
            odds = {
                "1": odds_data.get("home"),
                "X": odds_data.get("draw"),
                "2": odds_data.get("away")
            }
            
            clean_matches.append({
                "id": game_id,
                "match": f"{home} vs {away}",
                "odds": odds
            })
        except Exception:
            continue
            
    return clean_matches

# ==============================================================================
# ORCHESTRATEUR PRINCIPAL
# ==============================================================================
def run_scraper():
    print(f"[{time.strftime('%H:%M:%S')}] Lancement du scraping des 7 bookmakers...")
    results = {}
    
    for bk_name, bk_config in BOOKMAKERS_CONFIG.items():
        raw_json = fetch_raw_data(bk_name, bk_config)
        
        if raw_json:
            if bk_config["type"] == "betb2b":
                parsed = parse_betb2b(raw_json)
            else:
                parsed = parse_1win(raw_json)
                
            results[bk_name] = parsed
            print(f" -> {bk_name.upper()} : {len(parsed)} matchs récupérés.")
        else:
            results[bk_name] = []
            print(f" -> {bk_name.upper()} : Aucune donnée.")

    # Création du dossier docs si inexistant
    os.makedirs("docs", exist_ok=True)
    
    # Export JSON ultra-minifié (Zero spaces / Zero indents)
    output_path = "docs/odds_data.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, separators=(',', ':'))
        
    poids_ko = os.path.getsize(output_path) / 1024
    print(f"[{time.strftime('%H:%M:%S')}] Terminé. Fichier JSON généré: {poids_ko:.2f} Ko")

if __name__ == "__main__":
    run_scraper()

