import datetime
import json
import re

from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from config import (
    BOOKMAKERS,
    BOOKMAKERS_LIST,
    MAX_MATCHES_PER_SITE,
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

            page = context.new_page()

            scrape_bookmaker(
                page,
                bookmaker,
                BOOKMAKERS[
                    bookmaker
                ]
            )

            page.close()

        browser.close()


if __name__ == "__main__":
    main()
