use crate::geo::{bearing_deg, calc_twa, current_component, haversine_nm};
use crate::polar::PolarTable;
use crate::types::{BoatLimits, ForecastPoint, Waypoint, WaypointResult};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json;

// ─── Structures d'entrée/sortie ───────────────────────────────────────────────

#[derive(Deserialize)]
pub struct RouteInput {
    pub waypoints: Vec<Waypoint>,
    /// Prévisions — tableau de ForecastPoint (triés par waypoint_index puis forecast_time)
    pub forecasts: Vec<ForecastPoint>,
    /// Heure de départ UTC (ISO 8601) — utilisée pour aligner les prévisions
    pub departure_time: Option<String>,
    /// Vitesse fixe de secours si les polaires ne peuvent pas être utilisées
    pub boat_speed_fixed_kts: Option<f64>,
    pub limits: Option<BoatLimits>,
}

#[derive(Serialize)]
pub struct RouteOutput {
    pub total_distance_nm: f64,
    pub total_eta_hours: f64,
    pub avg_speed_kts: f64,
    pub avg_polar_speed_kts: f64,
    pub used_polars: bool,
    pub waypoints: Vec<WaypointResult>,
}

// ─── Commande CLI ─────────────────────────────────────────────────────────────

pub fn run(input: String, polars_path: &str) -> Result<String, String> {
    let inp: RouteInput = serde_json::from_str(&input)
        .map_err(|e| format!("JSON invalide pour la commande route: {}", e))?;

    let polar = PolarTable::load(polars_path).ok();

    let result = calculate_route(&inp, polar.as_ref())?;
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

// ─── Calcul de route ──────────────────────────────────────────────────────────

pub fn calculate_route(inp: &RouteInput, polar: Option<&PolarTable>) -> Result<RouteOutput, String> {
    if inp.waypoints.len() < 2 {
        return Err("Au moins 2 waypoints requis".to_string());
    }

    let speed_fallback = inp.boat_speed_fixed_kts.unwrap_or(6.0);
    let departure_epoch = inp
        .departure_time
        .as_deref()
        .and_then(|s| parse_iso(s))
        .unwrap_or(0);

    let limits = inp.limits.clone().unwrap_or_default();
    let mut results: Vec<WaypointResult> = Vec::new();
    let mut total_eta_h = 0.0_f64;
    let mut cumulative_nm = 0.0_f64;
    let mut polar_speeds: Vec<f64> = Vec::new();
    let mut used_polars = false;

    for i in 0..inp.waypoints.len() - 1 {
        let wp_from = &inp.waypoints[i];
        let wp_to = &inp.waypoints[i + 1];

        let dist_nm = haversine_nm(wp_from.lat, wp_from.lon, wp_to.lat, wp_to.lon);
        let brng = bearing_deg(wp_from.lat, wp_from.lon, wp_to.lat, wp_to.lon);

        // Prévision à ce waypoint pour l'heure d'arrivée estimée
        let fc_epoch = departure_epoch + (total_eta_h * 3600.0) as i64;
        let fc = find_forecast(&inp.forecasts, i, fc_epoch);

        // Calcul de vitesse via polaires
        let (boat_speed, twa, tws, used_polar) =
            if let (Some(p), Some(fc)) = (polar, fc.as_ref()) {
                let tws_val = fc.conditions.wind_speed_kts;
                let twd = fc.conditions.wind_direction_deg;
                let twa_val = calc_twa(twd, brng);
                let spd = p.get_speed(twa_val, tws_val);
                if spd >= 0.5 {
                    (spd, twa_val, tws_val, true)
                } else {
                    (speed_fallback, twa_val, tws_val, false)
                }
            } else {
                let twa_val = fc.as_ref().map(|f| calc_twa(f.conditions.wind_direction_deg, brng)).unwrap_or(90.0);
                let tws_val = fc.as_ref().map(|f| f.conditions.wind_speed_kts).unwrap_or(0.0);
                (speed_fallback, twa_val, tws_val, false)
            };

        if used_polar {
            used_polars = true;
        }
        polar_speeds.push(boat_speed);

        // Effet du courant
        let current_effect = fc.as_ref().and_then(|f| {
            let cs = f.conditions.current_speed_kts?;
            let cd = f.conditions.current_direction_deg?;
            Some(current_component(cs, cd, brng))
        }).unwrap_or(0.0);

        let effective_speed = (boat_speed + current_effect).max(0.1);
        let segment_eta_h = dist_nm / effective_speed;

        // Score de confort pour ce segment
        let comfort = fc.as_ref()
            .map(|f| calc_comfort(&f.conditions, brng, &limits))
            .unwrap_or(75.0);

        let result = WaypointResult {
            index: i,
            name: wp_from.name.clone(),
            lat: wp_from.lat,
            lon: wp_from.lon,
            distance_from_start_nm: cumulative_nm,
            bearing_deg: brng,
            boat_speed_kts: (boat_speed * 100.0).round() / 100.0,
            twa_deg: (twa * 10.0).round() / 10.0,
            tws_kts: (tws * 10.0).round() / 10.0,
            current_effect_kts: (current_effect * 100.0).round() / 100.0,
            effective_speed_kts: (effective_speed * 100.0).round() / 100.0,
            eta_hours: (total_eta_h * 10.0).round() / 10.0,
            comfort_score: (comfort * 10.0).round() / 10.0,
        };

        results.push(result);
        cumulative_nm += dist_nm;
        total_eta_h += segment_eta_h;
    }

    // Dernier waypoint (arrivée)
    let last = &inp.waypoints[inp.waypoints.len() - 1];
    results.push(WaypointResult {
        index: inp.waypoints.len() - 1,
        name: last.name.clone(),
        lat: last.lat,
        lon: last.lon,
        distance_from_start_nm: cumulative_nm,
        bearing_deg: results.last().map(|r| r.bearing_deg).unwrap_or(0.0),
        boat_speed_kts: 0.0,
        twa_deg: 0.0,
        tws_kts: 0.0,
        current_effect_kts: 0.0,
        effective_speed_kts: 0.0,
        eta_hours: (total_eta_h * 10.0).round() / 10.0,
        comfort_score: 0.0,
    });

    let avg_speed = if total_eta_h > 0.0 {
        cumulative_nm / total_eta_h
    } else {
        speed_fallback
    };
    let avg_polar = if polar_speeds.is_empty() {
        speed_fallback
    } else {
        polar_speeds.iter().sum::<f64>() / polar_speeds.len() as f64
    };

    Ok(RouteOutput {
        total_distance_nm: (cumulative_nm * 10.0).round() / 10.0,
        total_eta_hours: (total_eta_h * 10.0).round() / 10.0,
        avg_speed_kts: (avg_speed * 100.0).round() / 100.0,
        avg_polar_speed_kts: (avg_polar * 100.0).round() / 100.0,
        used_polars,
        waypoints: results,
    })
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/// Trouve la prévision la plus proche en temps pour ce waypoint
pub fn find_forecast(
    forecasts: &[ForecastPoint],
    wp_idx: usize,
    target_epoch: i64,
) -> Option<ForecastPoint> {
    // D'abord les prévisions exactes pour ce waypoint
    let wp_forecasts: Vec<&ForecastPoint> = forecasts
        .iter()
        .filter(|f| f.waypoint_index == wp_idx)
        .collect();

    // Si aucune prévision pour ce waypoint, prendre le waypoint précédent disponible
    let candidates = if wp_forecasts.is_empty() {
        // Fallback : prendre les prévisions du waypoint 0
        forecasts.iter().filter(|f| f.waypoint_index == 0).collect::<Vec<_>>()
    } else {
        wp_forecasts
    };

    if candidates.is_empty() {
        return None;
    }

    // Trouver la plus proche en temps
    candidates
        .into_iter()
        .min_by_key(|f| {
            let epoch = parse_iso(&f.forecast_time).unwrap_or(0);
            (epoch - target_epoch).abs()
        })
        .cloned()
}

/// Score de confort entre 0 et 100
pub fn calc_comfort(cond: &crate::types::WeatherCondition, boat_heading: f64, limits: &BoatLimits) -> f64 {
    let mut score = 100.0_f64;
    let wind = cond.wind_speed_kts;

    // Vent
    if wind > limits.wind_max_kts {
        score -= 50.0;
    } else if wind > 25.0 {
        score -= 25.0;
    } else if wind > limits.ideal_wind_max_kts {
        score -= 10.0;
    } else if wind < 5.0 {
        score -= 15.0;
    }

    // Direction vent vs cap (TWA)
    let twa = calc_twa(cond.wind_direction_deg, boat_heading);
    if twa < 30.0 {
        score -= 30.0; // Face au vent
    } else if twa < 60.0 {
        score -= 15.0; // Près
    } else if twa < 100.0 {
        score -= 5.0; // Travers
    }

    // Vagues
    if let Some(wave_h) = cond.wave_height_m {
        if wave_h > limits.wave_max_m {
            score -= 40.0;
        } else if wave_h > 2.0 {
            score -= 10.0;
        }
    }

    // Houle
    if let Some(swell_h) = cond.swell_height_m {
        if swell_h > limits.swell_max_m {
            score -= 30.0;
        }
        if let Some(swell_dir) = cond.swell_direction_deg {
            let swell_angle = calc_twa(swell_dir, boat_heading);
            if swell_angle > 60.0 && swell_angle < 120.0 {
                score -= 20.0; // Houle croisée
            }
        }
    }

    // Courant contraire
    if let (Some(cs), Some(cd)) = (cond.current_speed_kts, cond.current_direction_deg) {
        let comp = current_component(cs, cd, boat_heading);
        if comp < -1.0 {
            score -= 10.0;
        }
    }

    score.clamp(0.0, 100.0)
}

/// Parse une chaîne ISO 8601 → epoch seconds
pub fn parse_iso(s: &str) -> Option<i64> {
    let s = s.trim();
    // Tenter de parser avec chrono
    if let Ok(dt) = s.parse::<DateTime<Utc>>() {
        return Some(dt.timestamp());
    }
    // Tenter YYYY-MM-DDTHH:MM:SS sans timezone (supposé UTC)
    if let Ok(dt) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S") {
        return Some(dt.and_utc().timestamp());
    }
    // YYYY-MM-DD (minuit UTC)
    if let Ok(d) = chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d") {
        return Some(d.and_hms_opt(0, 0, 0)?.and_utc().timestamp());
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_iso() {
        let epoch = parse_iso("2026-03-11T00:00:00Z").unwrap();
        assert!(epoch > 0);
        // 2026-03-11 00:00:00 UTC
        assert_eq!(epoch, 1773187200);
    }

    #[test]
    fn test_route_no_forecast() {
        let inp = RouteInput {
            waypoints: vec![
                Waypoint { lat: 16.88, lon: -25.00, name: Some("Mindelo".into()) },
                Waypoint { lat: 13.07, lon: -59.62, name: Some("Barbade".into()) },
            ],
            forecasts: vec![],
            departure_time: None,
            boat_speed_fixed_kts: Some(6.0),
            limits: None,
        };
        let result = calculate_route(&inp, None).unwrap();
        // Distance ≈ 1980 NM à 6 kts ≈ 330h
        assert!(result.total_distance_nm > 1900.0);
        assert!(result.total_distance_nm < 2100.0);
        assert!(result.total_eta_hours > 300.0);
        assert!(result.total_eta_hours < 380.0);
    }
}
