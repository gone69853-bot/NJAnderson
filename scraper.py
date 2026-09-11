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
                let compte = 0;

                // Méthode principale : attribut ARIA standard, utilisé
                // par la quasi-totalité des composants d'accordéon
                // (React/Vue...), quel que soit le nom de classe CSS
                // propre à chaque site.
                document.querySelectorAll(
                    '[aria-expanded="false"]'
                ).forEach(el => {
                    el.click();
                    compte++;
                });

                // Repli : ancienne classe personnalisée déjà repérée
                // sur certains sites de ce réseau, au cas où un site
                // n'utilise pas aria-expanded.
                document.querySelectorAll(
                    '.ui-accordion-trigger__arrow:not([data-deja-clique])'
                ).forEach(f => {
                    f.setAttribute('data-deja-clique', '1');
                    f.closest('[class*="accordion-trigger"]')?.click();
                    compte++;
                });

                // Repli n°2 : sur Betwinner/Melbet/Megapari/Winwin/1xbet/
                // Paripesa, le bouton "flèche" qui replie chaque
                // championnat (ex. "UEFA Europa League (22)") porte
                // directement la classe "ui-accordion__trigger" /
                // "ui-accordion-trigger", SANS aria-expanded. On le
                // cible donc lui-même, en le marquant après le premier
                // clic pour ne jamais le re-cliquer (sinon on le
                // refermerait au tour suivant au lieu de le laisser
                // ouvert).
                document.querySelectorAll(
                    '[class*="accordion__trigger"]:not([data-deja-ouvert]), ' +
                    '[class*="accordion-trigger"]:not([data-deja-ouvert])'
                ).forEach(btn => {
                    btn.setAttribute('data-deja-ouvert', '1');
                    btn.click();
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


def click_maximize_buttons(page, bookmaker="", max_rounds=20):
    """
    Déploie les sections cachées derrière le bouton UI
    "Maximize" sur tous les bookmakers qui utilisent ce composant.

    Le sélecteur est volontairement précis pour ne pas cliquer sur
    d'autres contrôles :
      button.ui-nav-link-toggle[aria-label="Maximize"]
             [aria-expanded="false"]

    Si un bookmaker n'utilise pas ce bouton, la fonction ne fait rien.
    """
    selector = (
        'button.ui-nav-link-toggle[aria-label="Maximize"]'
        '[aria-expanded="false"]'
    )

    total_clicked = 0

    for tour in range(max_rounds):
        clicked_this_round = 0

        try:
            # Recompter à chaque tour : après un clic, le DOM peut être
            # recréé et de nouveaux boutons peuvent apparaître.
            count = page.locator(selector).count()

            for i in range(count):
                try:
                    btn = page.locator(selector).nth(i)

                    if btn.is_visible():
                        btn.click(timeout=3000, force=True)
                        clicked_this_round += 1
                        total_clicked += 1
                        page.wait_for_timeout(500)

                except Exception:
                    pass

            if clicked_this_round:
                page.wait_for_timeout(1800)

            # Faire apparaître les éventuelles sections situées plus bas.
            scroll_page(page)
            page.wait_for_timeout(800)

            if clicked_this_round:
                print(
                    f"[{bookmaker}] Maximize : "
                    f"{clicked_this_round} bouton(s) ouvert(s) "
                    f"(tour {tour + 1})"
                )
            else:
                # Deux tours sans bouton = probablement tout est déjà ouvert.
                if tour >= 1:
                    break

        except Exception as error:
            print(f"[{bookmaker}] erreur Maximize : {error}")
            break

    return total_clicked


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
    click_maximize_buttons(page, urlparse(listing_url).netloc)
    # Délai plus long ici : la première passe peut ouvrir plusieurs
    # dizaines de championnats d'un coup (ex. "UEFA Europa League (22)"),
    # chacun déclenchant son propre chargement de matchs.
    page.wait_for_timeout(4000)

    hrefs, found = count_match_links(page)

    stagnant = 0

    while found < max_matches and stagnant < max_stagnant:

        scroll_page(page)
        expand_accordions(page)
        click_maximize_buttons(page, urlparse(listing_url).netloc, max_rounds=3)

        page.wait_for_timeout(3000)

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
# 1WIN — moteur totalement différent (app Vue.js), et surtout
# pas besoin de proxy : l'IP US (celle du runner GitHub) n'est
# pas bloquée sur ce site, contrairement aux autres.
# ============================================================

WIN1_LISTING_URL = "https://1win.com/betting/prematch?platform_type=mobile"
WIN1_MAX_TENTATIVES = 300  # jusqu'à 10 min pour que les cartes se chargent

WIN1_MOTIF_COTE = re.compile(r"\d\.\d")


def extraire_cartes_1win(page):

    return page.evaluate(
        """
        () => {
            const cartes = document.querySelectorAll('[data-qa="match-card"]');
            const resultat = [];
            cartes.forEach(carte => {
                const teamsEl = carte.querySelector('[data-scope="TeamNames"]');
                const oddsEl = carte.querySelector('[data-qa="matchCardBaseOdds"]');
                resultat.push({
                    teamsText: teamsEl ? teamsEl.innerText : "",
                    oddsText: oddsEl ? oddsEl.innerText : ""
                });
            });
            return resultat;
        }
        """
    )


def parser_carte_1win(carte):

    lignes_equipes = [
        l.strip() for l in carte["teamsText"].split("\n") if l.strip()
    ]

    if len(lignes_equipes) < 2:
        return None

    equipe_1, equipe_2 = lignes_equipes[0], lignes_equipes[1]

    lignes_cotes = [
        l.strip() for l in carte["oddsText"].split("\n") if l.strip()
    ]

    resultat_1x2 = {}

    try:

        i = next(
            idx for idx, l in enumerate(lignes_cotes)
            if "full time result" in l.lower()
        )

        correspondance = {"1": "V1", "x": "X", "2": "V2"}
        pos = i + 1

        while pos + 1 < len(lignes_cotes):

            label = lignes_cotes[pos].strip().lower()
            valeur = lignes_cotes[pos + 1].strip()

            if label in correspondance:
                resultat_1x2[correspondance[label]] = valeur
                pos += 2
            else:
                break

    except StopIteration:
        pass

    if not resultat_1x2:
        return None

    return {

        "bookmaker": "1win",

        "equipe_1": equipe_1,

        "equipe_2": equipe_2,

        "1X2": resultat_1x2,

        "Total_2.5": {
            "Plus de": None,
            "Moins de": None,
        },

        "url": WIN1_LISTING_URL,

        "derniere_maj":
            datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),

        "statut": "ok",
    }


def ouvrir_plus_de_matchs_1win(page, max_tours=20):
    """
    1win masque une partie des compétitions derrière des boutons
    "Maximize". On clique explicitement sur ces boutons pour déployer
    davantage de matchs avant de lire les cartes.
    """
    precedent = -1
    sans_nouveau = 0

    for tour in range(max_tours):
        try:
            # Cible le bouton fourni par l'interface 1win :
            # <button aria-label="Maximize" ... class="ui-nav-link-toggle ...">
            cliques = page.locator(
                'button.ui-nav-link-toggle[aria-label="Maximize"]'
                '[aria-expanded="false"]'
            ).count()

            if cliques:
                for i in range(cliques):
                    try:
                        page.locator(
                            'button.ui-nav-link-toggle[aria-label="Maximize"]'
                            '[aria-expanded="false"]'
                        ).nth(i).click(
                            timeout=3000,
                            force=True
                        )
                    except Exception:
                        pass

                page.wait_for_timeout(1800)

            # Faire apparaître les sections éventuellement chargées plus bas.
            page.mouse.wheel(0, 5000)
            page.keyboard.press("End")
            page.wait_for_timeout(1200)

            nb_cartes = page.locator(
                '[data-qa="match-card"]'
            ).count()

            if nb_cartes <= precedent:
                sans_nouveau += 1
            else:
                sans_nouveau = 0
                print(
                    f"[1win] déploiement : {nb_cartes} carte(s)"
                )

            precedent = nb_cartes

            if sans_nouveau >= 3:
                break

        except Exception:
            break

    # Remonter en haut n'est pas nécessaire pour extraire les cartes :
    # le DOM contient aussi les cartes chargées hors écran.
    return precedent


def scrape_1win(playwright):

    result = []

    try:

        # Pas de proxy pour 1win : l'IP du runner n'est pas
        # bloquée sur ce site.
        browser = playwright.chromium.launch(headless=True)

        page = browser.new_page()

        page.goto(
            WIN1_LISTING_URL,
            timeout=1200000,
            wait_until="domcontentloaded"
        )

        # Laisser l'application Vue.js initialiser les championnats.
        page.wait_for_timeout(5000)

        # Même logique que pour les autres bookmakers : on ouvre
        # les sections cachées derrière les boutons "Maximize".
        click_maximize_buttons(page, "1win")

        cartes = []

        for tentative in range(WIN1_MAX_TENTATIVES):

            cartes = extraire_cartes_1win(page)

            nb_avec_cotes = sum(
                1 for c in cartes
                if WIN1_MOTIF_COTE.search(c["oddsText"])
            )

            if (
                len(cartes) > 0
                and nb_avec_cotes >= len(cartes) * 0.5
            ):

                print(
                    f"[1win] {len(cartes)} carte(s) détectée(s), "
                    f"{nb_avec_cotes} avec cotes, "
                    f"après {tentative * 2}s"
                )

                break

            page.wait_for_timeout(2000)

        browser.close()

        for carte in cartes:

            parsed = parser_carte_1win(carte)

            if parsed:
                result.append(parsed)

    except Exception as error:

        print(f"[1win] ERREUR : {error}")

    output = ROOT / "1win.json"

    output.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"[1win] "
        f"{len(result)} matchs enregistrés"
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
                # Traité séparément juste après (pas de proxy,
                # navigateur dédié).
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

        # ------------------------------------------------------
        # 1WIN : pas de proxy nécessaire (IP du runner non
        # bloquée), donc navigateur séparé sans configuration
        # proxy.
        # ------------------------------------------------------
        scrape_1win(playwright)


if __name__ == "__main__":
    main()
