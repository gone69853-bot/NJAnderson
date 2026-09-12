
import datetime
import json
import re
import unicodedata

from pathlib import Path
from difflib import SequenceMatcher


# ============================================================
# FICHES PAR MATCH (docs/{slug}/index.html)
# ============================================================

TEMPLATE_PATH = Path("templates/match_template.html")

# Fichier qui garde la trace des dossiers générés lors du
# dernier passage, pour pouvoir supprimer ceux des matchs qui
# ont disparu (terminés / plus suivis) plutôt que de les laisser
# s'accumuler indéfiniment dans docs/.
MANIFEST_PATH = Path("docs/.match-slugs.json")


def slugify(value):

    value = unicodedata.normalize(
        "NFKD",
        str(value or "")
    )

    value = value.encode(
        "ascii",
        "ignore"
    ).decode("ascii")

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value
    )

    return value.strip("-")


def match_slug(match):

    return (
        slugify(match.get("equipe_1"))
        + "-"
        + slugify(match.get("equipe_2"))
    )


def generate_match_pages(matches):

    if not TEMPLATE_PATH.exists():

        print(
            "templates/match_template.html introuvable, "
            "fiches par match ignorées"
        )
        return

    template = TEMPLATE_PATH.read_text(
        encoding="utf-8"
    )

    previous_slugs = []

    if MANIFEST_PATH.exists():

        try:

            previous_slugs = json.loads(
                MANIFEST_PATH.read_text(
                    encoding="utf-8"
                )
            )

        except Exception:

            previous_slugs = []

    current_slugs = []

    for match in matches:

        slug = match_slug(match)

        if not slug:
            continue

        current_slugs.append(slug)

        folder = Path("docs") / slug
        folder.mkdir(
            parents=True,
            exist_ok=True
        )

        equipe_1 = match.get("equipe_1", "?")
        equipe_2 = match.get("equipe_2", "?")

        title = (
            f"{equipe_1} – {equipe_2} · "
            "Cotes en direct | Zicote"
        )

        description = (
            f"Compare en direct les cotes de {equipe_1} - "
            f"{equipe_2} chez plusieurs bookmakers."
        )

        # Les balises </script> dans les données (peu probable
        # mais possible dans un nom d'équipe) casseraient le
        # bloc JSON embarqué : on les neutralise.
        match_json = json.dumps(
            match,
            ensure_ascii=False
        ).replace("</", "<\\/")

        html = (
            template
            .replace("__TITLE__", title)
            .replace("__DESCRIPTION__", description)
            .replace("__MATCH_JSON__", match_json)
        )

        (folder / "index.html").write_text(
            html,
            encoding="utf-8"
        )

    # Nettoyage des fiches qui ne correspondent plus à un match
    # actuel.
    for slug in previous_slugs:

        if slug in current_slugs:
            continue

        folder = Path("docs") / slug
        index_file = folder / "index.html"

        if index_file.exists():

            try:

                index_file.unlink()
                folder.rmdir()

            except Exception:

                pass

    MANIFEST_PATH.write_text(
        json.dumps(
            current_slugs,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        f"{len(current_slugs)} fiches de match générées"
    )


BOOKMAKERS = [
    "betwinner",
    "melbet",
    "megapari",
    "1win",
    "winwin",
    "1xbet",
    "paripesa",
    "africa-bizbet",
]


# ============================================================
# NORMALISATION
# ============================================================

def normalize(value):

    value = str(
        value or ""
    ).lower()

    replacements = {

        "é": "e",
        "è": "e",
        "ê": "e",
        "ë": "e",

        "à": "a",
        "â": "a",

        "î": "i",
        "ï": "i",

        "ô": "o",
        "ö": "o",

        "ù": "u",
        "û": "u",
        "ü": "u",

        "ç": "c",
    }

    for old, new in replacements.items():

        value = value.replace(
            old,
            new
        )

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value
    )

    return " ".join(
        value.split()
    )


# ============================================================
# SIMILARITE
# ============================================================

def similarity(a, b):

    a1 = normalize(
        a.get("equipe_1")
    )

    a2 = normalize(
        a.get("equipe_2")
    )

    b1 = normalize(
        b.get("equipe_1")
    )

    b2 = normalize(
        b.get("equipe_2")
    )

    direct = (
        SequenceMatcher(
            None,
            a1,
            b1
        ).ratio()
        +
        SequenceMatcher(
            None,
            a2,
            b2
        ).ratio()
    ) / 2

    inverse = (
        SequenceMatcher(
            None,
            a1,
            b2
        ).ratio()
        +
        SequenceMatcher(
            None,
            a2,
            b1
        ).ratio()
    ) / 2

    return max(
        direct,
        inverse
    )


# ============================================================
# CHARGEMENT
# ============================================================

def load_bookmaker(name):

    file = Path(
        f"{name}.json"
    )

    if not file.exists():

        return []

    try:

        data = json.loads(
            file.read_text(
                encoding="utf-8"
            )
        )

        return [
            x for x in data
            if x.get("statut") == "ok"
        ]

    except Exception:

        return []


# ============================================================
# REGROUPEMENT DES MATCHS
# ============================================================

def group_matches(data):

    groups = []

    for bookmaker in BOOKMAKERS:

        for match in data.get(
            bookmaker,
            []
        ):

            best_group = None

            best_score = 0

            for group in groups:

                for existing in group.values():

                    score = similarity(
                        match,
                        existing
                    )

                    if score > best_score:

                        best_score = score

                        best_group = group

            if best_group is not None and best_score >= 0.82:

                best_group[
                    bookmaker
                ] = match

            else:

                groups.append(
                    {
                        bookmaker: match
                    }
                )

    return groups


# ============================================================
# COMPARAISON
# ============================================================

def compare_market(
    group,
    market,
    keys=None,
    type_label=None
):
    # Si aucune liste de clés n'est fournie, on les découvre en
    # regardant ce que chaque bookmaker a effectivement renvoyé
    # pour ce marché (utile pour Handicap / Score exact, dont les
    # libellés varient d'un match à l'autre).
    if keys is None:

        keys = []

        for match in group.values():

            for key in (match.get(market) or {}).keys():

                if key not in keys:
                    keys.append(key)

    rows = []

    for key in keys:

        values = {}

        for bookmaker, match in group.items():

            try:

                value = match.get(
                    market,
                    {}
                ).get(
                    key
                )

                if value is not None:

                    values[
                        bookmaker
                    ] = float(
                        str(value).replace(
                            ",",
                            "."
                        )
                    )

            except Exception:

                continue

        if not values:

            continue

        best_bookmaker = max(
            values,
            key=values.get
        )

        rows.append({

            "marche": key,

            "type": type_label or market,

            "valeurs": values,

            "meilleur_site":
                best_bookmaker,

            "meilleure_cote":
                values[
                    best_bookmaker
                ],
        })

    return rows


def compare_totals_dynamic(group):
    """Cas particulier de 'Totals' : structure à deux niveaux
    ({"1.5": {"Plus de": .., "Moins de": ..}, "2": {...}, ...}).
    On aplatit en une ligne par (ligne de total, sens)."""

    lines = []

    for match in group.values():

        for line in (match.get("Totals") or {}).keys():

            if line not in lines:
                lines.append(line)

    rows = []

    for line in lines:

        for sens in ("Plus de", "Moins de"):

            values = {}

            for bookmaker, match in group.items():

                try:

                    value = (
                        match.get("Totals", {})
                        .get(line, {})
                        .get(sens)
                    )

                    if value is not None:

                        values[bookmaker] = float(
                            str(value).replace(",", ".")
                        )

                except Exception:

                    continue

            if not values:
                continue

            best_bookmaker = max(values, key=values.get)

            rows.append({

                "marche": f"{line}|{sens}",

                "type": "Totals",

                "valeurs": values,

                "meilleur_site": best_bookmaker,

                "meilleure_cote": values[best_bookmaker],
            })

    return rows


# ============================================================
# GENERATION
# ============================================================

def build():

    data = {}

    for bookmaker in BOOKMAKERS:

        data[
            bookmaker
        ] = load_bookmaker(
            bookmaker
        )

    groups = group_matches(
        data
    )

    output = []

    for group in groups:

        first = next(
            iter(group.values())
        )

        markets = []

        markets.extend(
            compare_market(
                group,
                "1X2",
                ["V1", "X", "V2"],
                "1X2"
            )
        )

        markets.extend(
            compare_market(
                group,
                "Total_2.5",
                ["Plus de", "Moins de"],
                "Total_2.5"
            )
        )

        markets.extend(
            compare_market(
                group,
                "Double_Chance",
                ["1X", "12", "2X"],
                "Double_Chance"
            )
        )

        markets.extend(
            compare_market(
                group,
                "BTTS",
                ["Oui", "Non"],
                "BTTS"
            )
        )

        markets.extend(
            compare_market(
                group,
                "Handicap",
                None,
                "Handicap"
            )
        )

        markets.extend(
            compare_market(
                group,
                "Score_Exact",
                None,
                "Score_Exact"
            )
        )

        markets.extend(
            compare_totals_dynamic(group)
        )

        if not markets:

            continue

        output.append({

            "equipe_1":
                first.get(
                    "equipe_1",
                    "?"
                ),

            "equipe_2":
                first.get(
                    "equipe_2",
                    "?"
                ),

            "bookmakers":
                list(group.keys()),

            "marches":
                markets,
        })

    payload = {

        "genere_le":
            datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),

        "matchs": output,
    }

    Path(
        "docs/comparison.json"
    ).write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(
                ",",
                ":"
            )
        ),
        encoding="utf-8"
    )

    print(
        f"{len(output)} matchs comparés"
    )

    generate_match_pages(output)


if __name__ == "__main__":

    build()



