use crate::geo::{bearing_deg, calc_twa, current_component, haversine_nm};
use crate::polar::PolarTable;
use crate::route::{calc_comfort, parse_iso};
use crate::types::{BoatLimits, DepartureResult, ForecastPoint, Waypoint};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json;
use std::collections::HashMap;

// ─── Structures d'entrée/sortie ───────────────────────────────────────────────

#[derive(Deserialize)]
pub struct ScoreInput {
    pub waypoints: Vec<Waypoint>,
    /// Vitesse de secours si polaires indisponibles
    pub boat_speed_avg_kts: Option<f64>,
    /// Dates de départ ISO 8601 (dates ou datetimes)
    pub departure_datetimes: Vec<String>,
    /// Prévisions par waypoint : "0" → liste de ForecastPoint
    pub forecasts: HashMap<String, Vec<ForecastPoint>>,
    pub limits: Option<BoatLimits>,
}

#[derive(Serialize)]
pub struct ScoreOutput {
    pub departures: Vec<DepartureResult>,
}

// ─── Commande CLI ─────────────────────────────────────────────────────────────

pub fn run(input: String, polars_path: &str) -> Result<String, String> {
    let inp: ScoreInput = serde_json::from_str(&input)
        .map_err(|e| format!("JSON invalide pour la commande score: {}", e))?;

    let polar = PolarTable::from_csv(polars_path).ok();

    let result = calculate_scores(&inp, polar.as_ref())?;
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

// ─── Calcul des scores en parallèle ──────────────────────────────────────────

pub fn calculate_scores(inp: &ScoreInput, polar: Option<&PolarTable>) -> Result<ScoreOutput, String> {
    if inp.waypoints.len() < 2 {
        return Err("Au moins 2 waypoints requis".to_string());
    }

    let speed_fallback = inp.boat_speed_avg_kts.unwrap_or(6.0);
    let limits = inp.limits.clone().unwrap_or_default();

    // Pré-calcul des prévisions indexées par (wp_idx, epoch)
    let forecasts_by_wp: HashMap<usize, Vec<(i64, ForecastPoint)>> = inp
        .forecasts
        .iter()
        .filter_map(|(k, v)| {
            let idx: usize = k.parse().ok()?;
            let mut timed: Vec<(i64, ForecastPoint)> = v
                .iter()
                .filter_map(|f| {
                    let epoch = parse_iso(&f.forecast_time)?;
                    Some((epoch, f.clone()))
                })
                .collect();
            timed.sort_by_key(|(e, _)| *e);
            Some((idx, timed))
        })
        .collect();

    // Simulation en parallèle via rayon
    let departures: Vec<DepartureResult> = inp
        .departure_datetimes
        .par_iter()
        .map(|dep_str| {
            let dep_epoch = parse_iso(dep_str).unwrap_or(0);
            simulate_departure(
                &inp.waypoints,
                dep_str,
                dep_epoch,
                &forecasts_by_wp,
                polar,
                speed_fallback,
                &limits,
            )
        })
        .collect();

    Ok(ScoreOutput { departures })
}

// ─── Simulation d'un départ ───────────────────────────────────────────────────

fn simulate_departure(
    waypoints: &[Waypoint],
    dep_str: &str,
    dep_epoch: i64,
    forecasts_by_wp: &HashMap<usize, Vec<(i64, ForecastPoint)>>,
    polar: Option<&PolarTable>,
    speed_fallback: f64,
    limits: &BoatLimits,
) -> DepartureResult {
    const STEP_H: f64 = 3.0; // pas de 3h (comme Python)
    const MAX_STEPS: usize = 800;

    let total_dist: f64 = waypoints.windows(2)
        .map(|w| haversine_nm(w[0].lat, w[0].lon, w[1].lat, w[1].lon))
        .sum();

    let max_steps = ((total_dist / speed_fallback.max(1.0) / STEP_H * 3.0) as usize)
        .max(80)
        .min(MAX_STEPS);

    let mut cur_lat = waypoints[0].lat;
    let mut cur_lon = waypoints[0].lon;
    let mut cur_wp_idx = 0usize;
    let mut elapsed_h = 0.0_f64;

    let mut conf_samples: Vec<f64> = Vec::new();
    let mut comfort_samples: Vec<f64> = Vec::new();
    let mut polar_speeds: Vec<f64> = Vec::new();
    let mut current_effects: Vec<f64> = Vec::new();
    let mut wind_speeds: Vec<f64> = Vec::new();
    let mut wave_heights: Vec<f64> = Vec::new();
    let mut alerts: Vec<String> = Vec::new();
    let mut used_polars = false;

    for _ in 0..max_steps {
        if cur_wp_idx >= waypoints.len() - 1 {
            break;
        }

        let next_wp = &waypoints[cur_wp_idx + 1];
        let hdg = bearing_deg(cur_lat, cur_lon, next_wp.lat, next_wp.lon);
        let current_epoch = dep_epoch + (elapsed_h * 3600.0) as i64;

        // Prévision la plus proche en temps pour ce waypoint
        let fc = find_forecast_at(forecasts_by_wp, cur_wp_idx, current_epoch);

        // Vitesse bateau
        let (boat_speed, used_polar_step) = if let (Some(p), Some(fc)) = (polar, fc.as_ref()) {
            let tws = fc.conditions.wind_speed_kts;
            let twd = fc.conditions.wind_direction_deg;
            let twa = calc_twa(twd, hdg);
            let spd = p.get_speed(twa, tws);
            if spd >= 1.0 {
                (spd, true)
            } else {
                (speed_fallback, false)
            }
        } else {
            (speed_fallback, false)
        };

        if used_polar_step {
            used_polars = true;
        }
        polar_speeds.push(boat_speed);

        // Scores
        if let Some(fc) = fc.as_ref() {
            let wind = fc.conditions.wind_speed_kts;
            let conf = calc_confidence_score(elapsed_h, 3.0, 51);
            let comf = calc_comfort(&fc.conditions, hdg, limits);

            conf_samples.push(conf);
            comfort_samples.push(comf);
            wind_speeds.push(wind);

            if let Some(wh) = fc.conditions.wave_height_m {
                wave_heights.push(wh);
            }

            // Effet courant
            if let (Some(cs), Some(cd)) = (
                fc.conditions.current_speed_kts,
                fc.conditions.current_direction_deg,
            ) {
                let comp = current_component(cs, cd, hdg);
                current_effects.push(comp);
            }

            // Alertes (dédupliquées)
            let day = (elapsed_h / 24.0) as usize;
            if wind > limits.wind_max_kts {
                let msg = format!("J+{}: Vent {:.0} nds > limite {:.0} nds", day, wind, limits.wind_max_kts);
                if !alerts.contains(&msg) && alerts.len() < 10 {
                    alerts.push(msg);
                }
            }
            if let Some(wh) = fc.conditions.wave_height_m {
                if wh > limits.wave_max_m {
                    let msg = format!("J+{}: Vagues {:.1}m > limite {:.1}m", day, wh, limits.wave_max_m);
                    if !alerts.contains(&msg) && alerts.len() < 10 {
                        alerts.push(msg);
                    }
                }
            }
        }

        // Déplacement
        let current_along = fc.as_ref().and_then(|f| {
            let cs = f.conditions.current_speed_kts?;
            let cd = f.conditions.current_direction_deg?;
            Some(current_component(cs, cd, hdg))
        }).unwrap_or(0.0);

        let dist_moved = ((boat_speed + current_along) * STEP_H).max(0.0);
        let dist_to_next = haversine_nm(cur_lat, cur_lon, next_wp.lat, next_wp.lon);

        if dist_moved >= dist_to_next {
            cur_lat = next_wp.lat;
            cur_lon = next_wp.lon;
            cur_wp_idx += 1;
        } else {
            let frac = dist_moved / dist_to_next;
            cur_lat += frac * (next_wp.lat - cur_lat);
            cur_lon += frac * (next_wp.lon - cur_lon);
        }

        elapsed_h += STEP_H;
    }

    if conf_samples.is_empty() {
        return DepartureResult {
            departure_date: dep_str.to_string(),
            confidence_score: 0.0,
            comfort_score: 0.0,
            overall_score: 0.0,
            adjusted_eta_hours: elapsed_h,
            avg_wind_kts: 0.0,
            max_wind_kts: 0.0,
            avg_wave_m: 0.0,
            avg_current_effect_kts: 0.0,
            avg_polar_speed_kts: speed_fallback,
            used_polars: false,
            alerts: vec!["Données de prévision insuffisantes".to_string()],
            verdict: "NO-GO".to_string(),
        };
    }

    let avg_conf = mean(&conf_samples);
    let avg_comf = mean(&comfort_samples);
    let min_comf = comfort_samples.iter().cloned().fold(f64::INFINITY, f64::min);
    let overall = (0.3 * avg_conf + 0.4 * avg_comf + 0.3 * min_comf).clamp(0.0, 100.0);

    let verdict = if overall >= 70.0 {
        "GO"
    } else if overall >= 40.0 {
        "ATTENTION"
    } else {
        "NO-GO"
    };

    DepartureResult {
        departure_date: dep_str.to_string(),
        confidence_score: round2(avg_conf),
        comfort_score: round2(avg_comf),
        overall_score: round2(overall),
        adjusted_eta_hours: round1(elapsed_h),
        avg_wind_kts: round2(mean(&wind_speeds)),
        max_wind_kts: round2(wind_speeds.iter().cloned().fold(f64::NEG_INFINITY, f64::max)),
        avg_wave_m: round2(if wave_heights.is_empty() { 0.0 } else { mean(&wave_heights) }),
        avg_current_effect_kts: round2(if current_effects.is_empty() { 0.0 } else { mean(&current_effects) }),
        avg_polar_speed_kts: round2(mean(&polar_speeds)),
        used_polars,
        alerts,
        verdict: verdict.to_string(),
    }
}

// ─── Score de confiance ───────────────────────────────────────────────────────

/// Confiance en fonction de l'horizon de prévision et de la dispersion ensemble
fn calc_confidence_score(
    forecast_hour: f64,
    ensemble_std: f64,
    _n_members: usize,
) -> f64 {
    // Spread ensemble (40%)
    let ensemble_score = if ensemble_std < 2.0 {
        100.0
    } else if ensemble_std > 10.0 {
        0.0
    } else {
        100.0 - (ensemble_std - 2.0) * (100.0 / 8.0)
    };

    // Horizon temporel (60% — principal facteur sans données multi-modèles)
    let horizon_score = if forecast_hour <= 48.0 {
        100.0
    } else if forecast_hour <= 120.0 {
        75.0
    } else if forecast_hour <= 192.0 {
        50.0
    } else if forecast_hour <= 264.0 {
        25.0
    } else {
        10.0
    };

    (0.4 * ensemble_score + 0.6 * horizon_score).clamp(0.0, 100.0)
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn find_forecast_at(
    forecasts_by_wp: &HashMap<usize, Vec<(i64, ForecastPoint)>>,
    wp_idx: usize,
    target_epoch: i64,
) -> Option<ForecastPoint> {
    // Essayer ce waypoint, puis le précédent, puis 0
    for idx in [wp_idx, wp_idx.saturating_sub(1), 0] {
        if let Some(list) = forecasts_by_wp.get(&idx) {
            if list.is_empty() {
                continue;
            }
            let best = list
                .iter()
                .min_by_key(|(e, _)| (*e - target_epoch).abs())?;
            return Some(best.1.clone());
        }
    }
    None
}

fn mean(v: &[f64]) -> f64 {
    if v.is_empty() {
        0.0
    } else {
        v.iter().sum::<f64>() / v.len() as f64
    }
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_confidence_short_horizon() {
        let c = calc_confidence_score(24.0, 2.0, 51);
        assert!(c > 90.0, "Confiance horizon court: {}", c);
    }

    #[test]
    fn test_confidence_long_horizon() {
        let c = calc_confidence_score(300.0, 5.0, 51);
        assert!(c < 50.0, "Confiance horizon long: {}", c);
    }
}
