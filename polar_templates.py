#!/usr/bin/env python3
"""
polar_templates.py — Modèles de polaires pour différents types de bateaux.
Utilisé lors de l'inscription pour initialiser la polar_matrix du nouvel utilisateur.
"""

import math

# Grille TWA/TWS standard
TWA_GRID = [0, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155, 160, 165, 170, 175, 180]
TWS_GRID = [4, 6, 8, 10, 12, 14, 16, 18, 20, 25, 30, 35, 40]


def _sloop_croisiere_speed(twa_deg: float, tws_kts: float) -> float:
    """
    Polaire sloop croisière ~38ft (Bénéteau Oceanis, Jeanneau Sun Odyssey).
    Hull speed ~7.6 kts. Performances réalistes.
    Cibles : TWS=14, TWA=90° → 7.2 kts ; TWA=45° → 5.8 kts ; TWA=160° → 6.0 kts
    """
    if twa_deg == 0:
        return 0.0

    # Vitesse de coque
    hull_speed = 7.6

    # Facteur de vent : monte rapidement jusqu'à ~14 kts puis plafonne
    if tws_kts <= 0:
        return 0.0
    wind_factor = math.sqrt(tws_kts / 14.0)
    wind_factor = min(wind_factor, 1.0 + (tws_kts - 14) * 0.012) if tws_kts > 14 else wind_factor

    # Facteur angulaire — polaire typique monocoque croisière
    twa_r = math.radians(twa_deg)

    # Zones polaires
    if twa_deg < 30:
        # Sous le vent mort — quasi impossible de remonter
        angle_factor = 0.0 + (twa_deg / 30.0) * 0.45
    elif twa_deg <= 45:
        # Près serré — mauvais pour un croiseur
        t = (twa_deg - 30) / 15.0
        angle_factor = 0.45 + t * (0.76 - 0.45)
    elif twa_deg <= 60:
        # Près — bon équilibre
        t = (twa_deg - 45) / 15.0
        angle_factor = 0.76 + t * (0.88 - 0.76)
    elif twa_deg <= 90:
        # Travers — meilleure allure
        t = (twa_deg - 60) / 30.0
        angle_factor = 0.88 + t * (0.945 - 0.88)
    elif twa_deg <= 120:
        # Largue — excellent
        t = (twa_deg - 90) / 30.0
        angle_factor = 0.945 + t * (0.88 - 0.945)
    elif twa_deg <= 150:
        # Grand largue
        t = (twa_deg - 120) / 30.0
        angle_factor = 0.88 + t * (0.79 - 0.88)
    elif twa_deg <= 165:
        # Vent arrière — meilleur
        t = (twa_deg - 150) / 15.0
        angle_factor = 0.79 + t * (0.805 - 0.79)
    else:
        # Plein vent arrière
        t = (twa_deg - 165) / 15.0
        angle_factor = 0.805 - t * (0.805 - 0.785)

    # Vitesse de base
    base_speed = hull_speed * angle_factor * wind_factor

    # Cap sur la vitesse de coque pour les monocoques
    speed = min(base_speed, hull_speed * 1.05)

    # Correction pour petits vents (en dessous de 6 kts)
    if tws_kts < 6:
        speed *= (tws_kts / 6.0) ** 0.7

    return max(0.0, round(speed, 2))


def _make_polar_rows(speed_func, multiplier=1.0, hull_speed_cap=None):
    """Génère la liste complète des (twa, tws, speed) pour la grille standard."""
    rows = []
    for tws in TWS_GRID:
        for twa in TWA_GRID:
            spd = speed_func(twa, tws) * multiplier
            if hull_speed_cap is not None:
                spd = min(spd, hull_speed_cap)
            spd = max(0.0, round(spd, 2))
            rows.append((twa, tws, spd))
    return rows


def _catamaran_speed(twa_deg: float, tws_kts: float) -> float:
    """
    Polaire catamaran 40ft. Pas de vitesse de coque, 40% plus rapide qu'un sloop croisière.
    Cibles : TWS=14, TWA=90° → 10.5 kts ; TWA=45° → 7.5 kts ; TWA=160° → 9.5 kts
    """
    if twa_deg == 0:
        return 0.0
    if tws_kts <= 0:
        return 0.0

    # Facteur vent — cata plus sensible aux petits vents, plafonne moins
    wind_factor = math.sqrt(tws_kts / 14.0)
    if tws_kts > 14:
        wind_factor = wind_factor * (1.0 + (tws_kts - 14) * 0.018)

    # Facteur angulaire — cata plus performant au portant et grand largue
    if twa_deg < 30:
        angle_factor = 0.0 + (twa_deg / 30.0) * 0.38
    elif twa_deg <= 45:
        t = (twa_deg - 30) / 15.0
        angle_factor = 0.38 + t * (0.72 - 0.38)
    elif twa_deg <= 60:
        t = (twa_deg - 45) / 15.0
        angle_factor = 0.72 + t * (0.88 - 0.72)
    elif twa_deg <= 90:
        t = (twa_deg - 60) / 30.0
        angle_factor = 0.88 + t * (1.0 - 0.88)
    elif twa_deg <= 120:
        t = (twa_deg - 90) / 30.0
        angle_factor = 1.0 + t * (0.97 - 1.0)
    elif twa_deg <= 150:
        t = (twa_deg - 120) / 30.0
        angle_factor = 0.97 + t * (0.91 - 0.97)
    elif twa_deg <= 165:
        t = (twa_deg - 150) / 15.0
        angle_factor = 0.91 + t * (0.905 - 0.91)
    else:
        t = (twa_deg - 165) / 15.0
        angle_factor = 0.905 - t * 0.005

    base_speed = 10.5 * angle_factor * wind_factor

    if tws_kts < 6:
        base_speed *= (tws_kts / 6.0) ** 0.6

    return max(0.0, round(base_speed, 2))


# Référence vitesse sloop à TWS=14, TWA=90° pour recalage
_REF_SLOOP_90_14 = _sloop_croisiere_speed(90, 14)


def _build_templates():
    """Construit le dictionnaire POLAR_TEMPLATES."""

    # Sloop croisière (référence)
    sloop_rows = _make_polar_rows(_sloop_croisiere_speed, multiplier=1.0, hull_speed_cap=8.0)

    # Catamaran
    cata_rows = _make_polar_rows(_catamaran_speed, multiplier=1.0, hull_speed_cap=None)

    # Ketch : 8% plus lent que sloop_croisiere
    ketch_rows = _make_polar_rows(_sloop_croisiere_speed, multiplier=0.92, hull_speed_cap=7.5)

    # Sloop performance : 18% plus rapide que sloop_croisiere
    sloop_perf_rows = _make_polar_rows(_sloop_croisiere_speed, multiplier=1.18, hull_speed_cap=9.5)

    # Grand croiseur : 12% plus lent que sloop_croisiere (50ft+ lourd)
    grand_rows = _make_polar_rows(_sloop_croisiere_speed, multiplier=0.88, hull_speed_cap=7.8)

    return {
        'sloop_croisiere': {
            'name': 'Sloop croisière (~38ft)',
            'description': 'Monocoque croisière 11-12m, 7-9 tonnes. Bénéteau Oceanis, Jeanneau Sun Odyssey…',
            'rows': sloop_rows,
        },
        'catamaran': {
            'name': 'Catamaran (~40ft)',
            'description': 'Catamaran croisière 12m. Lagoon, Leopard, Fountaine Pajot… 40% plus rapide qu\'un mono.',
            'rows': cata_rows,
        },
        'ketch': {
            'name': 'Ketch croisière (~42ft)',
            'description': 'Ketch ou yawl de croisière 12-13m. Plus lourd, plus de surface de toile au portant.',
            'rows': ketch_rows,
        },
        'sloop_performance': {
            'name': 'Sloop performance (~35ft)',
            'description': 'Monocoque léger de sport-croisière 10-11m. First 35, Figaro 3, RM 1060…',
            'rows': sloop_perf_rows,
        },
        'grand_croiseur': {
            'name': 'Grand croiseur (~50ft)',
            'description': 'Monocoque lourd 15-16m, 15-20 tonnes. Oyster 545, Hanse 548, X-Yachts 50…',
            'rows': grand_rows,
        },
    }


POLAR_TEMPLATES = _build_templates()


if __name__ == "__main__":
    # Test rapide
    for key, tmpl in POLAR_TEMPLATES.items():
        rows = tmpl['rows']
        print(f"\n{key} — {tmpl['name']}")
        print(f"  {len(rows)} rows (expected 416)")
        # Chercher TWS=14
        for twa_target, tws_target in [(90, 14), (45, 14), (160, 14)]:
            row = next((r for r in rows if r[0] == twa_target and r[1] == tws_target), None)
            if row:
                print(f"  TWA={twa_target}°, TWS={tws_target}kts → {row[2]} kts")
