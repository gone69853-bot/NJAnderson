import datetime
import json
import re

import requests

from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from config import (
    BOOKMAKERS,
    BOOKMAKERS_LIST,
    MAX_MATCHES_PER_SITE,
    PROXY_SERVER,
    PROXY_USERNAME,
    PROXY_PASSWORD,
    proxy_config,
)


# ============================================================
# DOSSIER DES DONNEES
# ============================================================

ROOT = Path(".")


# ============================================================
# RESSOURCES A BLOQUER
# ============================================================

BLOCKED_TYPES = {
    "image",
    "media",
    "font",
    "stylesheet",
    "manifest",
    "texttrack",
}


# ============================================================
# ANALYTICS / TRACKERS / PUBLICITE
# ============================================================

BLOCKED_DOMAINS = (

    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "facebook.net",
    "hotjar",
    "clarity.ms",
    "segment.io",
    "mixpanel",
    "amplitude",
    "sentry.io",
    "analytics",
    "telemetry",
    "tracking",
    "tracker",
    "adservice",
    "adsystem",
    "pixel",
)


# ============================================================
# FILTRE RESEAU
# ============================================================

def should_block(route):

    request = route.request

    resource_type = request.resource_type
    url = request.url.lower()

    # Images, vidéos, fonts, CSS...
    if resource_type in BLOCKED_TYPES:
        return True

    # Trackers
    for domain in BLOCKED_DOMAINS:
        if domain in url:
            return True

    return False


# ============================================================
# PROXY POUR REQUESTS (format différent de celui de Playwright)
# ============================================================

def requests_proxies():

    if not PROXY_PASSWORD:
        return None

    # PROXY_SERVER est du type "http://p.webshare.io:80"
    auth_url = PROXY_SERVER.replace(
        "http://",
        f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@"
    )

    return {
        "http": auth_url,
        "https": auth_url,
    }


# ============================================================
# API INTERNE (moteur type "1xBet") — méthode principale
# ============================================================
#
# Betwinner, Melbet, Megapari, Paripesa, Winwin, 1xbet tournent
# tous sur le même moteur (marque blanche façon 1xBet), qui
# expose une API JSON non-officielle mais publique pour la
# liste des matchs. On l'essaie en premier : plus rapide, plus
# fiable, et ça évite de scraper du texte rendu par le JS.
# ============================================================

API_METHOD_PATHS = (
    "/service-api/LineFeed/Get1x2_VZip",
    "/LineFeed/Get1x2_VZip",
)


def fetch_api_matches(
    bookmaker,
    listing_url,
    max_matches
):

    parsed = urlparse(listing_url)

    domain = f"{parsed.scheme}://{parsed.netloc}"

    lang = "fr" if "/fr" in parsed.path else "en"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Referer": listing_url,
        "X-Requested-With": "XMLHttpRequest",
    }

    params = {
        "sports": "1",
        "count": str(max_matches),
        "lng": lang,
        "mode": "4",
        "country": "71",
        "partner": "1",
        "getEmpty": "true",
        "tf": "2200000",
    }

    proxies = requests_proxies()

    for path in API_METHOD_PATHS:

        url = domain + path

        data = None

        try:

            response = requests.get(
                url,
                params=params,
                headers=headers,
                proxies=proxies,
                timeout=20,
            )

            # On garde toujours une trace brute, même si le
            # parsing échoue ensuite : ça permet d'ajuster le
            # mapping des champs si la structure diffère.
            debug_api_file = ROOT / f"debug_api_{bookmaker}.json"

            if not debug_api_file.exists():

                debug_api_file.write_text(
                    response.text[:20000],
                    encoding="utf-8"
                )

            data = response.json()

        except Exception:

            continue

        values = (
            data.get("Value")
            if isinstance(data, dict)
            else None
        )

        if not values:
            continue

        matches = []

        for game in values:

            team1 = game.get("O1")
            team2 = game.get("O2")

            if not team1 or not team2:
                continue

            odds = {
                "V1": None,
                "X": None,
                "V2": None,
            }

            for bet in (game.get("E") or []):

                bet_type = bet.get("T")
                coeff = bet.get("C")

                if coeff is None:
                    continue

                if bet_type == 1:
                    odds["V1"] = str(coeff)

                elif bet_type == 2:
                    odds["X"] = str(coeff)

                elif bet_type == 3:
                    odds["V2"] = str(coeff)

            if not any(odds.values()):
                continue

            matches.append({

                "bookmaker": bookmaker,

                "equipe_1": team1,

                "equipe_2": team2,

                "1X2": odds,

                "Total_2.5": {
                    "Plus de": None,
                    "Moins de": None,
                },

                "url": listing_url,

                "derniere_maj":
                    datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),

                "statut": "ok",
            })

            if len(matches) >= max_matches:
                break

        if matches:
            return matches

    return []


# ============================================================
# EXTRACTION DES EQUIPES
# ============================================================

def extract_teams(text):

    text = text.strip()

    if not text:
        return None, None

    # Nettoyage
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    separators = [
        " vs ",
        " VS ",
        " - ",
        " – ",
        " — ",
    ]

    for separator in separators:

        if separator in text:

            parts = text.split(
                separator,
                1
            )

            if len(parts) == 2:

                return (
                    parts[0].strip(),
                    parts[1].strip()
                )

    return None, None


# ============================================================
# PARSING DES COTES
# ============================================================

def extract_odds(text):

    result = {
        "V1": None,
        "X": None,
        "V2": None,
        "Plus de": None,
        "Moins de": None,
    }

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    for i, line in enumerate(lines):

        normalized = line.lower()

        # 1
        if normalized == "1":
            if i + 1 < len(lines):

                value = lines[i + 1]

                if re.fullmatch(
                    r"\d+(?:[.,]\d+)?",
                    value
                ):
                    result["V1"] = value.replace(
                        ",",
                        "."
                    )

        # X
        elif normalized == "x":

            if i + 1 < len(lines):

                value = lines[i + 1]

                if re.fullmatch(
                    r"\d+(?:[.,]\d+)?",
                    value
                ):
                    result["X"] = value.replace(
                        ",",
                        "."
                    )

        # 2
        elif normalized == "2":

            if i + 1 < len(lines):

                value = lines[i + 1]

                if re.fullmatch(
                    r"\d+(?:[.,]\d+)?",
                    value
                ):
                    result["V2"] = value.replace(
                        ",",
                        "."
                    )

        # Plus de
        elif (
            "plus de 2.5" in normalized
            or "plus de 2,5" in normalized
        ):

            if i + 1 < len(lines):

                value = lines[i + 1]

                if re.fullmatch(
                    r"\d+(?:[.,]\d+)?",
                    value
                ):
                    result["Plus de"] = value.replace(
                        ",",
                        "."
                    )

        # Moins de
        elif (
            "moins de 2.5" in normalized
            or "moins de 2,5" in normalized
        ):

            if i + 1 < len(lines):

                value = lines[i + 1]

                if re.fullmatch(
                    r"\d+(?:[.,]\d+)?",
                    value
                ):
                    result["Moins de"] = value.replace(
                        ",",
                        "."
                    )

    return result


# ============================================================
# LECTURE DES COTES DIRECTEMENT SUR LA PAGE DE LISTING
# ============================================================
#
# Sur mobile, la page "/line" affiche déjà chaque match avec
# ses cotes en clair (W1 / DRAW / W2 / HANDICAP), sans avoir
# besoin d'ouvrir la page du match. On scanne le texte complet
# de la page pour repérer ces blocs.
# ============================================================

JUNK_KEYWORDS = (
    "round",
    "phase",
    "group",
    "leg",
    "final",
    "qualif",
    "play-off",
    "playoff",
    "groupe",
    "tour",
    "journee",
    "journée",
)

MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def is_junk_line(line):

    low = line.lower()

    # Nombre seul (badge, compteur de marchés...)
    if re.fullmatch(r"\d+(?:[.,]\d+)?", line):
        return True

    # Heure / date type 09/09 ou 17:45
    if re.match(r"^\d{1,2}[:/]\d{1,2}", line):
        return True

    if any(month in low for month in MONTHS):
        return True

    if any(word in low for word in JUNK_KEYWORDS):
        return True

    if len(line) < 2:
        return True

    return False


def find_teams_before(lines, index, lookback=8):

    candidates = []

    j = index - 1
    steps = 0

    while j >= 0 and steps < lookback and len(candidates) < 2:

        line = lines[j]

        if not is_junk_line(line):
            candidates.append(line)

        j -= 1
        steps += 1

    if len(candidates) < 2:
        return None, None

    # candidates[0] = ligne la plus proche du marqueur "W1"
    # (donc l'équipe 2), candidates[1] = l'équipe 1
    team2, team1 = candidates[0], candidates[1]

    return team1, team2


def extract_matches_from_listing(
    body,
    bookmaker,
    source_url,
    max_matches
):

    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip()
    ]

    n = len(lines)

    matches = []

    i = 0

    while i < n and len(matches) < max_matches:

        normalized = lines[i].strip().lower()

        is_v1_marker = normalized in ("1", "w1")

        has_next_number = (
            i + 1 < n
            and re.fullmatch(
                r"\d+(?:[.,]\d+)?",
                lines[i + 1]
            )
        )

        if is_v1_marker and has_next_number:

            v1 = lines[i + 1].replace(",", ".")

            x_val = None
            v2_val = None

            j = i + 2
            limit = min(n, i + 14)

            while j < limit - 1:

                nline = lines[j].strip().lower()

                if (
                    x_val is None
                    and nline in ("x", "draw", "nul")
                    and re.fullmatch(
                        r"\d+(?:[.,]\d+)?",
                        lines[j + 1]
                    )
                ):
                    x_val = lines[j + 1].replace(",", ".")
                    j += 2
                    continue

                if (
                    nline in ("2", "w2")
                    and re.fullmatch(
                        r"\d+(?:[.,]\d+)?",
                        lines[j + 1]
                    )
                ):
                    v2_val = lines[j + 1].replace(",", ".")
                    j += 2
                    break

                j += 1

            if x_val and v2_val:

                team1, team2 = find_teams_before(lines, i)

                if team1 and team2:

                    matches.append({

                        "bookmaker": bookmaker,

                        "equipe_1": team1,

                        "equipe_2": team2,

                        "1X2": {
                            "V1": v1,
                            "X": x_val,
                            "V2": v2_val,
                        },

                        "Total_2.5": {
                            "Plus de": None,
                            "Moins de": None,
                        },

                        "url": source_url,

                        "derniere_maj":
                            datetime.datetime.now(
                                datetime.timezone.utc
                            ).isoformat(),

                        "statut": "ok",
                    })

            i = j

        else:

            i += 1

    return matches


# ============================================================
# EXTRACTION D'UNE PAGE
# ============================================================

def scrape_page(
    page,
    bookmaker,
    url
):

    try:

        print(
            f"[{bookmaker}] {url}"
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        # Délai plus long : les cotes sont chargées par
        # un appel JS/websocket après le rendu initial,
        # 1.5s ne suffit pas sur ces sites.
        page.wait_for_timeout(4000)

        body = page.inner_text(
            "body"
        )

        if not body:
            return None

        # ------------------------------------------------------
        # MODE DEBUG : sauvegarde le texte brut de la 1ère page
        # de chaque bookmaker dans un fichier debug_<site>.txt.
        # Sert uniquement à inspecter la vraie structure de la
        # page pour affiner extract_odds ensuite. Sans danger,
        # à retirer une fois le parsing calé.
        # ------------------------------------------------------
        debug_file = ROOT / f"debug_{bookmaker}.txt"

        if not debug_file.exists():

            debug_file.write_text(
                body,
                encoding="utf-8"
            )

        # Recherche d'un titre de match
        lines = [
            x.strip()
            for x in body.splitlines()
            if x.strip()
        ]

        team1 = None
        team2 = None

        for line in lines:

            a, b = extract_teams(
                line
            )

            if a and b:

                team1 = a
                team2 = b

                break

        odds = extract_odds(
            body
        )

        # Il faut au minimum une cote
        if not any(
            value is not None
            for value in odds.values()
        ):
            return None

        return {

            "bookmaker": bookmaker,

            "equipe_1": team1,

            "equipe_2": team2,

            "1X2": {

                "V1": odds["V1"],

                "X": odds["X"],

                "V2": odds["V2"],
            },

            "Total_2.5": {

                "Plus de": odds["Plus de"],

                "Moins de": odds["Moins de"],
            },

            "url": url,

            "derniere_maj":
                datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),

            "statut": "ok",
        }

    except Exception as error:

        print(
            f"[{bookmaker}] erreur : {error}"
        )

        return None


# ============================================================
# SCRAPER D'UN BOOKMAKER
# ============================================================

def scrape_bookmaker(
    page,
    bookmaker,
    config
):

    url = config["url"]

    result = []

    try:

        # Petit retry : ces domaines coupent parfois la
        # connexion au premier essai (proxy tournant / anti-bot).
        last_error = None

        for attempt in range(2):

            try:

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                last_error = None

                break

            except Exception as goto_error:

                last_error = goto_error

                page.wait_for_timeout(2000)

        if last_error:
            raise last_error

        page.wait_for_timeout(
            2500
        )

        # ------------------------------------------------------
        # ETAPE 1 : lire les cotes directement sur la page de
        # listing (méthode principale, confirmée sur mobile :
        # W1 / DRAW / W2 apparaissent en clair à côté de chaque
        # match, pas besoin d'ouvrir la page du match).
        # ------------------------------------------------------
        listing_body = page.inner_text("body")

        debug_listing_file = ROOT / f"debug_listing_{bookmaker}.txt"

        if listing_body and not debug_listing_file.exists():

            debug_listing_file.write_text(
                listing_body,
                encoding="utf-8"
            )

        if listing_body:

            result = extract_matches_from_listing(
                listing_body,
                bookmaker,
                url,
                MAX_MATCHES_PER_SITE
            )

        print(
            f"[{bookmaker}] "
            f"{len(result)} matchs (listing)"
        )

        if result:

            output = ROOT / f"{bookmaker}.json"

            output.write_text(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2
                ),
                encoding="utf-8"
            )

            print(
                f"[{bookmaker}] "
                f"{len(result)} matchs enregistrés"
            )

            return

        # ------------------------------------------------------
        # ETAPE 2 (fallback) : si la lecture directe n'a rien
        # donné (structure de page différente), on retombe sur
        # l'ancienne méthode : visiter chaque page de match.
        # ------------------------------------------------------

        # Récupération de tous les liens de la page.
        # On ne coupe PAS à 20 ici : les vrais liens de
        # matchs sont souvent loin dans le DOM (après tout
        # le menu, les jeux, le footer...).
        # Sur certains sites (SPA type 1win), la page continue
        # de naviguer/re-render après le domcontentloaded, ce
        # qui casse eval_on_selector_all ("Execution context
        # was destroyed"). On retente une fois après une pause.
        links = []

        for attempt in range(2):

            try:

                links = page.eval_on_selector_all(
                    "a",
                    """
                    elements =>
                        elements
                        .map(e => e.href)
                        .filter(Boolean)
                    """
                )

                break

            except Exception:

                page.wait_for_timeout(2000)

        # Un lien de match a la forme :
        # .../line/<sport>/<id-competition>-<slug>/<id-match>-<equipe1>-<equipe2>
        # (2 segments qui commencent par un id numérique)
        match_pattern = re.compile(
            r"/\d+-[a-z0-9-]+/\d+-[a-z0-9-]+/?$"
        )

        candidate_links = [
            link for link in links
            if match_pattern.search(link.lower())
        ]

        # Si le filtre ne trouve rien (site différent,
        # structure inconnue...), on retombe sur l'ancien
        # comportement pour ne pas se retrouver bredouille.
        source_links = (
            candidate_links
            if candidate_links
            else links
        )

        unique_links = []

        for link in source_links:

            if link not in unique_links:

                unique_links.append(
                    link
                )

            if len(unique_links) >= MAX_MATCHES_PER_SITE:

                break

        print(
            f"[{bookmaker}] "
            f"{len(unique_links)} liens"
        )

        for link in unique_links:

            match = scrape_page(
                page,
                bookmaker,
                link
            )

            if match:

                result.append(
                    match
                )

            if len(result) >= MAX_MATCHES_PER_SITE:

                break

    except Exception as error:

        print(
            f"[{bookmaker}] "
            f"ERREUR : {error}"
        )

    output = ROOT / f"{bookmaker}.json"

    output.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"[{bookmaker}] "
        f"{len(result)} matchs enregistrés"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    proxy = proxy_config()

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True,
            proxy=proxy
        )

        context = browser.new_context(
            viewport={
                "width": 390,
                "height": 844
            },

            java_script_enabled=True,

            service_workers="block",

            locale="fr-FR",
        )

        # BLOQUER LES RESSOURCES INUTILES
        context.route(
            "**/*",
            lambda route:
                route.abort()
                if should_block(route)
                else route.continue_()
        )

        for bookmaker in BOOKMAKERS_LIST:

            config = BOOKMAKERS[bookmaker]

            url = config["url"]

            # ------------------------------------------------------
            # ETAPE 0 : tentative via l'API interne du moteur
            # (pas de navigateur nécessaire, rapide et fiable
            # quand ça fonctionne).
            # ------------------------------------------------------
            api_matches = []

            try:

                api_matches = fetch_api_matches(
                    bookmaker,
                    url,
                    MAX_MATCHES_PER_SITE
                )

            except Exception as error:

                print(
                    f"[{bookmaker}] "
                    f"erreur API : {error}"
                )

            print(
                f"[{bookmaker}] "
                f"{len(api_matches)} matchs (API)"
            )

            if api_matches:

                output = ROOT / f"{bookmaker}.json"

                output.write_text(
                    json.dumps(
                        api_matches,
                        ensure_ascii=False,
                        indent=2
                    ),
                    encoding="utf-8"
                )

                print(
                    f"[{bookmaker}] "
                    f"{len(api_matches)} matchs enregistrés"
                )

                continue

            # ------------------------------------------------------
            # ETAPE 1/2 (fallback) : navigateur (listing puis
            # pages de match individuelles).
            # ------------------------------------------------------
            page = context.new_page()

            scrape_bookmaker(
                page,
                bookmaker,
                config
            )

            page.close()

        browser.close()


if __name__ == "__main__":
    main()
