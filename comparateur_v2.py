import json
import re

from pathlib import Path
from difflib import SequenceMatcher


BOOKMAKERS = [
    "betwinner",
    "melbet",
    "megapari",
    "1win",
    "winwin",
    "1xbet",
    "paripesa",
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
    keys
):

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

            "valeurs": values,

            "meilleur_site":
                best_bookmaker,

            "meilleure_cote":
                values[
                    best_bookmaker
                ],
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
                [
                    "V1",
                    "X",
                    "V2"
                ]
            )
        )

        markets.extend(
            compare_market(
                group,
                "Total_2.5",
                [
                    "Plus de",
                    "Moins de"
                ]
            )
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

    Path(
        "comparison.json"
    ).write_text(
        json.dumps(
            output,
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


if __name__ == "__main__":

    build()
