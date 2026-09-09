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
#
# On NE bloque PAS les feuilles de style : certains de ces sites
# s'appuient sur le layout réel (dimensions/visibilité calculées
# via CSS) pour déclencher le chargement de la liste des matchs
# (listes virtualisées). Bloquer le CSS cassait ce déclenchement.
# ============================================================

BLOCKED_TYPES = {
    "image",
    "media",
    "font",
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


def should_block(route):

    request = route.request

    resource_type = request.resource_type
    url = request.url.lower()

    if resource_type in BLOCKED_TYPES:
        return True

    for domain in BLOCKED_DOMAINS:
        if domain in url:
            return True

    return False


# ============================================================
# MOTIF DE DETECTION DES LIENS DE MATCH
# ============================================================
#
# Format "classique" (Betwinner, Melbet, Megapari, Paripesa,
# Winwin, 1xbet) : /line/football/ID-championnat/ID-equipe1-equipe2
# ============================================================

MATCH_PATTERN = re.compile(r"/line/football/\d+-[^/]+/\d+-[^/?]+")


def with_mobile_param(url):

    if "platform_type=mobile" in url:
        return url

    separator = "&" if "?" in url else "?"

    return f"{url}{separator}platform_type=mobile"


def extract_teams_from_slug(href):
    """Best-effort : extrait les noms d'équipes depuis le slug de
    l'URL (.../ID-equipe1-equipe2), utilisé en repli si la page
    elle-même n'affiche pas clairement les noms."""

    try:

        match = re.search(r"/\d+-([^/?]+)$", href)

        if match:

            slug = match.group(1)
            parts = slug.split("-")

            if len(parts) >= 2:

                middle = len(parts) // 2

                team1 = " ".join(parts[:middle]).title()
                team2 = " ".join(parts[middle:]).title()

                return team1, team2

    except Exception:
        pass

    return None, None


# ============================================================
# DECOUVERTE DES MATCHS SUR LA PAGE DE LISTING
# ============================================================
#
# On scrolle (molette + touche Fin + JS + scroll des conteneurs
# internes, combinés car les sites ne réagissent pas tous au
# même déclencheur) et on déplie les accordéons de championnats
# repliés, jusqu'à avoir assez de liens de match ou jusqu'à ce
# que ça stagne.
# ============================================================

def expand_accordions(page):

    try:

        return page.evaluate(
            """
            () => {
                const fleches = document.querySelectorAll(
                    '.ui-accordion-trigger__arrow:not([data-deja-clique])'
                );
                let compte = 0;
                fleches.forEach(f => {
                    f.setAttribute('data-deja-clique', '1');
                    f.closest('[class*="accordion-trigger"]')?.click();
                    compte++;
                });
                return compte;
            }
            """
        )

    except Exception:
        return 0


def scroll_page(page):

    try:
        page.mouse.wheel(0, 4000)
    except Exception:
        pass

    try:
        page.keyboard.press("End")
    except Exception:
        pass

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass

    try:

        page.evaluate(
            """
            () => {
                document.querySelectorAll('div').forEach(el => {
                    if (el.scrollHeight > el.clientHeight + 50) {
                        el.scrollTop = el.scrollHeight;
                    }
                });
            }
            """
        )

    except Exception:
        pass


def count_match_links(page):

    hrefs = page.eval_on_selector_all(
        "a",
        "els => els.map(e => e.getAttribute('href'))"
    )

    count = sum(
        1 for h in hrefs
        if h and MATCH_PATTERN.search(h)
    )

    return hrefs, count


def discover_matches(
    page,
    listing_url,
    max_matches,
    max_stagnant=40
):

    url = with_mobile_param(listing_url)

    page.goto(
        url,
        timeout=180000,
        wait_until="domcontentloaded"
    )

    # Laisser le premier lot de matchs se charger.
    page.wait_for_timeout(15000)

    expand_accordions(page)
    page.wait_for_timeout(2000)

    hrefs, found = count_match_links(page)

    stagnant = 0

    while found < max_matches and stagnant < max_stagnant:

        scroll_page(page)
        expand_accordions(page)

        page.wait_for_timeout(2500)

        hrefs, new_found = count_match_links(page)

        if new_found <= found:
            stagnant += 1
        else:
            stagnant = 0

        found = new_found

    parsed = urlparse(listing_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    matches = []
    seen = set()

    for href in hrefs:

        if not href:
            continue

        if MATCH_PATTERN.search(href):

            full_url = (
                href if href.startswith("http")
                else base_url + href
            )

            if full_url not in seen:

                seen.add(full_url)

                team1, team2 = extract_teams_from_slug(href)

                matches.append({
                    "url": full_url,
                    "equipe_1": team1,
                    "equipe_2": team2,
                })

        if len(matches) >= max_matches:
            break

    return matches


# ============================================================
# PARSING DES COTES SUR LA PAGE DE MATCH
# ============================================================
#
# On cherche la ligne exacte "1X2" puis on prend les 3 paires
# (libellé, cote) qui suivent — peu importe si le libellé exact
# est "1"/"X"/"2" ou "V1"/"Draw"/"V2", l'ORDRE (victoire équipe 1,
# nul, victoire équipe 2) est toujours le même sur ces sites.
# ============================================================

def parse_1x2_block(text):

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    try:
        i = lines.index("1X2")
    except ValueError:
        return None

    block = {}
    pos = i + 1

    for _ in range(3):

        if pos + 1 < len(lines):
            block[lines[pos]] = lines[pos + 1]
            pos += 2

    return block if len(block) == 3 else None


def parse_total_block(text):

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    try:
        i_total = lines.index("Total")
    except ValueError:
        return {"Plus de": None, "Moins de": None}

    for j in range(i_total, min(i_total + 60, len(lines) - 3)):

        if lines[j] == "2.5 Plus de":

            plus = lines[j + 1]

            moins = (
                lines[j + 3]
                if (
                    j + 2 < len(lines)
                    and lines[j + 2] == "2.5 Moins de"
                )
                else None
            )

            return {"Plus de": plus, "Moins de": moins}

    return {"Plus de": None, "Moins de": None}


def scrape_match(
    page,
    bookmaker,
    match,
    max_wait_cycles,
    nb_essais=2
):

    for attempt in range(nb_essais):

        try:

            page.goto(
                match["url"],
                timeout=120000,
                wait_until="domcontentloaded"
            )

            text = ""
            found = False

            for _ in range(max_wait_cycles):

                text = page.inner_text("body")

                if "1X2" in text:
                    found = True
                    break

                page.wait_for_timeout(2000)

            if not found:
                continue

            block = parse_1x2_block(text)

            if not block:
                continue

            values = list(block.values())

            odds = {
                "V1": values[0],
                "X": values[1],
                "V2": values[2],
            }

            total = parse_total_block(text)

            return {

                "bookmaker": bookmaker,

                "equipe_1": match.get("equipe_1"),

                "equipe_2": match.get("equipe_2"),

                "1X2": odds,

                "Total_2.5": total,

                "url": match["url"],

                "derniere_maj":
                    datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),

                "statut": "ok",
            }

        except Exception:
            continue

    return None


# ============================================================
# SCRAPER D'UN BOOKMAKER
# ============================================================

def scrape_bookmaker(
    page,
    bookmaker,
    config,
    max_matches,
    max_wait_cycles
):

    listing_url = config["url"]

    result = []
    matches = []

    try:

        for attempt in range(2):

            try:

                matches = discover_matches(
                    page,
                    listing_url,
                    max_matches
                )

                break

            except Exception as error:

                print(
                    f"[{bookmaker}] "
                    f"erreur découverte (essai {attempt + 1}/2) : "
                    f"{error}"
                )

                page.wait_for_timeout(3000)

        print(
            f"[{bookmaker}] "
            f"{len(matches)} match(s) découvert(s)"
        )

        for match in matches:

            data = scrape_match(
                page,
                bookmaker,
                match,
                max_wait_cycles
            )

            if data:
                result.append(data)

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
        f"{len(result)} matchs enregistrés "
        f"(sur {len(matches)} découverts)"
    )


# ============================================================
# MAIN
# ============================================================

# Nombre de cycles d'attente (2s chacun) avant d'abandonner un
# match si les cotes "1X2" ne s'affichent pas. 40 cycles = 80s
# max par match — un compromis entre patience et durée totale
# du run (avec plusieurs sites x plusieurs matchs, 90 cycles
# comme dans le script d'origine ferait un run bien trop long
# en CI si plusieurs matchs échouent).
MAX_WAIT_CYCLES = 40


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

        context.route(
            "**/*",
            lambda route:
                route.abort()
                if should_block(route)
                else route.continue_()
        )

        for bookmaker in BOOKMAKERS_LIST:

            if bookmaker == "1win":
                # Structure totalement différente (app Vue.js),
                # gérée séparément par test_1win_v2.py.
                print("[1win] ignoré (voir test_1win_v2.py)")
                continue

            config = BOOKMAKERS[bookmaker]

            page = context.new_page()

            scrape_bookmaker(
                page,
                bookmaker,
                config,
                MAX_MATCHES_PER_SITE,
                MAX_WAIT_CYCLES
            )

            page.close()

        browser.close()


if __name__ == "__main__":
    main()
