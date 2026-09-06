from playwright.sync_api import sync_playwright
import json
import datetime

LISTING_URL = "https://1win.com/betting/prematch?platform_type=mobile"
MAX_TENTATIVES = 300  # 10 minutes max pour que les cartes de match apparaissent


import re

def extraire_cartes(page):
    """Extrait teamsText et oddsText de chaque carte de match visible."""
    return page.evaluate("""
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
    """)


MOTIF_COTE = re.compile(r"\d\.\d")  # détecte une vraie cote type "3.36"


def parser_carte(carte):
    lignes_equipes = [l.strip() for l in carte["teamsText"].split("\n") if l.strip()]
    if len(lignes_equipes) < 2:
        return None
    equipe_1, equipe_2 = lignes_equipes[0], lignes_equipes[1]

    lignes_cotes = [l.strip() for l in carte["oddsText"].split("\n") if l.strip()]

    resultat_1x2 = {}
    try:
        i = next(idx for idx, l in enumerate(lignes_cotes) if "full time result" in l.lower())
        # Après le titre : label, valeur, label, valeur, label, valeur
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
        "equipe_1": equipe_1,
        "equipe_2": equipe_2,
        "1X2": resultat_1x2,
        "derniere_maj": datetime.datetime.now().isoformat(timespec="seconds"),
        "statut": "ok",
    }


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        print("Chargement de la page de listing...")
        page.goto(LISTING_URL, timeout=1200000, wait_until="domcontentloaded")

        cartes = []
        for tentative in range(MAX_TENTATIVES):
            cartes = extraire_cartes(page)
            nb_avec_cotes = sum(1 for c in cartes if MOTIF_COTE.search(c["oddsText"]))
            if len(cartes) > 0 and nb_avec_cotes >= len(cartes) * 0.5:
                print(f"{len(cartes)} carte(s) détectée(s), {nb_avec_cotes} avec cotes chargées, après {tentative*2}s")
                break
            if tentative % 15 == 0 and tentative > 0:
                print(f"... toujours en attente ({tentative*2}s écoulées) — {len(cartes)} cartes, {nb_avec_cotes} avec cotes")
            page.wait_for_timeout(2000)

        browser.close()

    resultats = []
    for i, carte in enumerate(cartes):
        if i < 3:  # affiche le détail des 3 premières cartes pour diagnostic
            print(f"\n--- Carte {i} : teamsText ---")
            print(repr(carte["teamsText"]))
            print(f"--- Carte {i} : oddsText ---")
            print(repr(carte["oddsText"]))
        parsed = parser_carte(carte)
        if parsed:
            resultats.append(parsed)

    with open("1win.json", "w", encoding="utf-8") as f:
        json.dump(resultats, f, ensure_ascii=False, indent=2)

    print(f"\n{len(resultats)} match(s) avec cotes valides sauvegardés dans 1win.json")


if __name__ == "__main__":
    main()
