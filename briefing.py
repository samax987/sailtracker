#!/usr/bin/env python3
"""
briefing.py — Génération de briefing météo en langage marin professionnel
"""

import math


CARDINALS = [
    'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSO', 'SO', 'OSO', 'O', 'ONO', 'NO', 'NNO'
]


def deg_to_cardinal(degrees):
    """Convertit un angle en point cardinal (16 points, en français)."""
    if degrees is None:
        return '—'
    idx = round(degrees / 22.5) % 16
    return CARDINALS[idx]


def circular_mean(angles_deg):
    """Moyenne circulaire d'une liste d'angles en degrés."""
    if not angles_deg:
        return 0
    sins = [math.sin(math.radians(a)) for a in angles_deg]
    coss = [math.cos(math.radians(a)) for a in angles_deg]
    return (math.degrees(math.atan2(sum(sins)/len(sins), sum(coss)/len(coss))) + 360) % 360


def bearing(lat1, lon1, lat2, lon2):
    """Cap initial en degrés entre deux points (degrés vrais)."""
    la1, lo1 = math.radians(lat1), math.radians(lon1)
    la2, lo2 = math.radians(lat2), math.radians(lon2)
    dlon = lo2 - lo1
    x = math.sin(dlon) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def get_allure(wind_dir_deg, route_bearing_deg):
    """
    Retourne l'allure de navigation en français.
    wind_dir_deg : direction d'où vient le vent (convention météo)
    route_bearing_deg : cap du navire
    """
    # Angle apparent du vent (0 = vent de face)
    awa = (wind_dir_deg - route_bearing_deg + 360) % 360
    if awa > 180:
        awa = 360 - awa  # symétrie
    if awa < 45:
        return 'face au vent'
    elif awa < 75:
        return 'au près serré'
    elif awa < 95:
        return 'au près'
    elif awa < 120:
        return 'au largue'
    elif awa < 150:
        return 'grand largue'
    else:
        return 'vent arrière'


def sea_qualifier(wave_m):
    """Qualificatif de l'état de la mer."""
    if wave_m is None or wave_m < 0.5:
        return 'belle'
    elif wave_m < 1.25:
        return 'peu agitée'
    elif wave_m < 2.0:
        return 'agitée'
    elif wave_m < 3.0:
        return 'forte'
    else:
        return 'très forte'


def wind_qualifier(speed_kts):
    """Qualificatif du vent."""
    if speed_kts < 8:
        return 'calme'
    elif speed_kts < 14:
        return 'légère brise'
    elif speed_kts < 21:
        return 'jolie brise'
    elif speed_kts < 28:
        return 'bonne brise'
    elif speed_kts < 34:
        return 'vent frais'
    else:
        return 'coup de vent'


def generate_weather_briefing(waypoints_data, route_info, best_departure_date=None, best_score=None):
    """
    Génère un briefing météo en langage marin professionnel.

    waypoints_data : liste de dicts {lat, lon, wind_speed, wind_dir, wave_height, current_speed, nm_from_start}
    route_info : dict {total_nm, route_bearing}
    best_departure_date : str ISO (optionnel)
    best_score : float (optionnel)

    Retourne : {summary, phases:[{name, range, text}], alerts:[str]}
    """
    if not waypoints_data:
        return {
            'summary': 'Données météo insuffisantes pour générer un briefing.',
            'phases': [],
            'alerts': []
        }

    total_nm = route_info.get('total_nm', 0)
    route_bearing = route_info.get('route_bearing', 0)

    # ── Statistiques globales ──
    winds = [w['wind_speed'] for w in waypoints_data if w.get('wind_speed') is not None]
    dirs = [w['wind_dir'] for w in waypoints_data if w.get('wind_dir') is not None]
    waves = [w['wave_height'] for w in waypoints_data if w.get('wave_height') is not None]
    currents = [w['current_speed'] for w in waypoints_data if w.get('current_speed') is not None]

    avg_wind = sum(winds) / len(winds) if winds else 0
    max_wind = max(winds) if winds else 0
    min_wind = min(winds) if winds else 0
    dom_dir = circular_mean(dirs) if dirs else 0
    dom_card = deg_to_cardinal(dom_dir)
    avg_wave = sum(waves) / len(waves) if waves else 0
    avg_current = sum(currents) / len(currents) if currents else 0

    # Tendance (première vs dernière moitié)
    mid = len(winds) // 2
    trend_text = ''
    if mid > 0 and len(winds) > 2:
        first_avg = sum(winds[:mid]) / mid
        last_avg = sum(winds[mid:]) / (len(winds) - mid)
        delta = last_avg - first_avg
        if delta > 3:
            trend_text = ', avec renforcement progressif en fin de route'
        elif delta < -3:
            trend_text = ', avec atténuation progressive en fin de route'
        else:
            trend_text = ', conditions stables sur l\'ensemble du parcours'

    # Régime
    is_alizes = (30 <= dom_dir <= 100) and (10 <= avg_wind <= 28)
    if is_alizes:
        regime = f'Régime d\'alizés de {dom_card} ({wind_qualifier(avg_wind)})'
    elif avg_wind < 8:
        regime = f'Conditions calmes — vents faibles de {dom_card}'
    elif avg_wind > 25:
        regime = f'Régime perturbé — vent fort de {dom_card}'
    else:
        regime = f'Vent de {dom_card} modéré à frais'

    allure = get_allure(dom_dir, route_bearing)

    summary = (
        f"{regime}, {avg_wind:.0f} nœuds en moyenne (max {max_wind:.0f} nœuds){trend_text}. "
        f"Navigation principalement {allure}. "
        f"Mer {sea_qualifier(avg_wave)} ({avg_wave:.1f} m en moyenne)."
    )

    if best_departure_date and best_score is not None:
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(best_departure_date)
            date_str = dt.strftime('%d %B %Y')
        except Exception:
            date_str = best_departure_date
        summary += f" Conditions optimales le {date_str} (score {best_score:.0f}/100)."

    # ── Phases (5 tranches) ──
    PHASE_DEFS = [
        ('Départ', 0.00, 0.20),
        ('Début traversée', 0.20, 0.45),
        ('Mi-route', 0.45, 0.65),
        ('Approche finale', 0.65, 0.85),
        ('Arrivée', 0.85, 1.00),
    ]

    phases = []
    for name, frac_start, frac_end in PHASE_DEFS:
        nm_start = total_nm * frac_start
        nm_end = total_nm * frac_end
        segment = [
            w for w in waypoints_data
            if nm_start <= w.get('nm_from_start', 0) <= nm_end
        ]
        if not segment:
            # Prendre les waypoints les plus proches
            segment = waypoints_data

        seg_winds = [w['wind_speed'] for w in segment if w.get('wind_speed') is not None]
        seg_dirs = [w['wind_dir'] for w in segment if w.get('wind_dir') is not None]
        seg_waves = [w['wave_height'] for w in segment if w.get('wave_height') is not None]
        seg_currents = [w['current_speed'] for w in segment if w.get('current_speed') is not None]

        seg_wind_avg = sum(seg_winds) / len(seg_winds) if seg_winds else avg_wind
        seg_dir = circular_mean(seg_dirs) if seg_dirs else dom_dir
        seg_wave = sum(seg_waves) / len(seg_waves) if seg_waves else avg_wave
        seg_current = sum(seg_currents) / len(seg_currents) if seg_currents else avg_current

        seg_allure = get_allure(seg_dir, route_bearing)
        seg_card = deg_to_cardinal(seg_dir)

        text = (
            f"Vent {seg_card} {seg_wind_avg:.0f} nœuds ({wind_qualifier(seg_wind_avg)}), "
            f"mer {sea_qualifier(seg_wave)}. "
            f"Navigation {seg_allure}."
        )
        if seg_current > 0.3:
            text += f" Courant portant {seg_current:.1f} nœuds."
        elif seg_current < -0.3:
            text += f" Courant défavorable {abs(seg_current):.1f} nœuds."

        phases.append({
            'name': name,
            'range': f'{nm_start:.0f}–{nm_end:.0f} NM',
            'text': text,
        })

    # ── Alertes ──
    alerts = []
    if min_wind < 8:
        alerts.append(f'⚠️ Risque de calme plat ({min_wind:.0f} nœuds minimum) — prévoir moteur')
    if max_wind > 25:
        alerts.append(f'⚠️ Rafales fortes attendues jusqu\'à {max_wind:.0f} nœuds — prendre un ris préventif')
    if avg_current > 0.8:
        alerts.append(f'ℹ️ Courant portant notable en moyenne ({avg_current:.1f} nœuds) — gain de distance')
    if avg_wave > 2.5:
        alerts.append(f'⚠️ Mer forte ({avg_wave:.1f} m en moyenne) — navigation inconfortable possible')

    return {
        'summary': summary,
        'phases': phases,
        'alerts': alerts,
    }
