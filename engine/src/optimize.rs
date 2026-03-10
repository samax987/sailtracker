use crate::geo::{bearing_deg, calc_twa, current_component, destination_point, haversine_nm};
use crate::polar::PolarTable;
use crate::route::parse_iso;
use crate::types::BoatLimits;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json;
use std::time::Instant;

// ─── Structures d'entrée/sortie ───────────────────────────────────────────────

#[derive(Deserialize)]
pub struct OptimizeInput {
    pub start: GeoPointIn,
    pub end: GeoPointIn,
    pub departure_time: String,
    pub wind_grid: Vec<WindGridSlot>,
    pub max_deviation_nm: Option<f64>,
    pub angle_step_deg: Option<f64>,
    pub time_step_hours: Option<f64>,
    pub limits: Option<BoatLimits>,
    pub boat_speed_fixed_kts: Option<f64>,
}

#[derive(Deserialize, Clone)]
pub struct GeoPointIn {
    pub lat: f64,
    pub lon: f64,
}

#[derive(Deserialize, Clone)]
pub struct WindGridSlot {
    pub time: String,
    pub points: Vec<WindPoint>,
}

#[derive(Deserialize, Clone)]
pub struct WindPoint {
    pub lat: f64,
    pub lon: f64,
    pub wind_speed_kts: f64,
    pub wind_dir_deg: f64,
    pub current_speed_kts: Option<f64>,
    pub current_dir_deg: Option<f64>,
}

#[derive(Serialize, Clone)]
pub struct RouteWaypoint {
    pub lat: f64,
    pub lon: f64,
    pub time: String,
    pub speed_kts: f64,
    pub bearing_deg: f64,
}

#[derive(Serialize)]
pub struct OptimizeOutput {
    pub status: String,
    pub direct_route: DirectRoute,
    pub optimized_route: OptimizedRoute,
    pub gain_hours: f64,
    pub gain_percent: f64,
    pub computation_time_ms: u64,
    pub isochrones_count: usize,
    pub points_evaluated: u64,
}

#[derive(Serialize)]
pub struct DirectRoute {
    pub distance_nm: f64,
    pub eta_hours: f64,
}

#[derive(Serialize)]
pub struct OptimizedRoute {
    pub distance_nm: f64,
    pub eta_hours: f64,
    pub waypoints: Vec<RouteWaypoint>,
}

// ─── Détection des terres (bounding boxes) ───────────────────────────────────

/// Tuples : (lat_min, lat_max, lon_min, lon_max)
const LAND_BOXES: &[(f64, f64, f64, f64)] = &[
    // Îles Canaries
    (27.5, 29.5, -18.5, -13.0),
    // Cap-Vert
    (14.5, 17.5, -25.5, -22.5),
    // Madère
    (32.5, 33.5, -17.5, -16.0),
    // Açores
    (36.5, 40.0, -32.0, -24.5),
    // Antilles (zone générale)
    (10.0, 19.0, -63.5, -59.0),
    (15.0, 22.0, -74.0, -60.0),
    // Côte africaine (zone générale)
    (0.0, 35.0, -18.0, -12.0),
    // Amérique du Sud côte est
    (5.0, 15.0, -60.0, -35.0),
];

fn is_on_land(lat: f64, lon: f64) -> bool {
    for &(lat_min, lat_max, lon_min, lon_max) in LAND_BOXES {
        if lat >= lat_min && lat <= lat_max && lon >= lon_min && lon <= lon_max {
            return true;
        }
    }
    false
}

// ─── Point sur le front d'isochrone ──────────────────────────────────────────

#[derive(Clone)]
struct FrontPoint {
    lat: f64,
    lon: f64,
    elapsed_h: f64,
    dist_to_end: f64,
    /// Indices dans l'historique (pour reconstruction du chemin)
    path: Vec<(f64, f64)>,
}

// ─── Commande CLI ─────────────────────────────────────────────────────────────

pub fn run(input: String, polars_path: &str) -> Result<String, String> {
    let inp: OptimizeInput = serde_json::from_str(&input)
        .map_err(|e| format!("JSON invalide pour optimize: {}", e))?;

    let polar = PolarTable::from_csv(polars_path).ok();
    let result = isochrone_optimize(&inp, polar.as_ref())?;
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

// ─── Algorithme des isochrones ────────────────────────────────────────────────

pub fn isochrone_optimize(inp: &OptimizeInput, polar: Option<&PolarTable>) -> Result<OptimizeOutput, String> {
    let t_start = Instant::now();

    let departure_epoch = parse_iso(&inp.departure_time)
        .ok_or("Impossible de parser departure_time")?;

    let angle_step = inp.angle_step_deg.unwrap_or(5.0);
    let time_step = inp.time_step_hours.unwrap_or(1.0);
    let max_deviation = inp.max_deviation_nm.unwrap_or(300.0);
    let limits = inp.limits.clone().unwrap_or_default();
    let speed_fallback = inp.boat_speed_fixed_kts.unwrap_or(6.0);

    let end_lat = inp.end.lat;
    let end_lon = inp.end.lon;

    let direct_dist = haversine_nm(inp.start.lat, inp.start.lon, end_lat, end_lon);
    let direct_bearing = bearing_deg(inp.start.lat, inp.start.lon, end_lat, end_lon);

    // ETA direct avec vitesse de base
    let direct_eta = estimate_direct_eta(
        inp.start.lat, inp.start.lon,
        end_lat, end_lon,
        &inp.wind_grid,
        departure_epoch,
        polar,
        speed_fallback,
        &limits,
    );

    // Pré-calculer les angles de déviation testés
    let angles: Vec<f64> = {
        let n_steps = (180.0 / angle_step) as i32;
        (-n_steps..=n_steps)
            .map(|i| i as f64 * angle_step)
            .collect()
    };

    // Front initial
    let mut front = vec![FrontPoint {
        lat: inp.start.lat,
        lon: inp.start.lon,
        elapsed_h: 0.0,
        dist_to_end: direct_dist,
        path: vec![(inp.start.lat, inp.start.lon)],
    }];

    let mut best_finish: Option<FrontPoint> = None;
    let mut isochrones_count = 0usize;
    let mut points_evaluated = 0u64;

    let max_iter = (direct_eta * 1.5 / time_step) as usize + 20;

    for _step in 0..max_iter {
        if front.is_empty() {
            break;
        }

        // Propager tous les points du front en parallèle
        let new_candidates: Vec<FrontPoint> = front
            .par_iter()
            .flat_map(|pt| {
                let mut candidates = Vec::new();
                let current_epoch =
                    departure_epoch + (pt.elapsed_h * 3600.0) as i64;
                let wind = get_wind_at(&inp.wind_grid, pt.lat, pt.lon, current_epoch);

                for &dev_angle in &angles {
                    let bearing = crate::geo::normalize_angle(direct_bearing + dev_angle);

                    // Calcul de la vitesse
                    let (speed, ok) = calc_speed(
                        &wind,
                        bearing,
                        polar,
                        speed_fallback,
                        &limits,
                    );
                    if !ok {
                        continue; // Conditions dépassent les limites
                    }

                    let dist_moved = speed * time_step;
                    let (new_lat, new_lon) = destination_point(pt.lat, pt.lon, bearing, dist_moved);

                    // Filtres
                    if is_on_land(new_lat, new_lon) {
                        continue;
                    }

                    // Distance au départ pour vérifier la déviation max
                    let dist_from_direct = dist_from_direct_line(
                        new_lat, new_lon,
                        inp.start.lat, inp.start.lon,
                        end_lat, end_lon,
                    );
                    if dist_from_direct > max_deviation {
                        continue;
                    }

                    let dist_to_end = haversine_nm(new_lat, new_lon, end_lat, end_lon);
                    let mut new_path = pt.path.clone();
                    new_path.push((new_lat, new_lon));

                    candidates.push(FrontPoint {
                        lat: new_lat,
                        lon: new_lon,
                        elapsed_h: pt.elapsed_h + time_step,
                        dist_to_end,
                        path: new_path,
                    });
                }
                candidates
            })
            .collect();

        points_evaluated += new_candidates.len() as u64;

        // Vérifier si on a atteint l'arrivée
        for pt in &new_candidates {
            if pt.dist_to_end < 10.0 {
                if best_finish.is_none()
                    || pt.elapsed_h < best_finish.as_ref().unwrap().elapsed_h
                {
                    best_finish = Some(pt.clone());
                }
            }
        }

        if best_finish.is_some() {
            // Une route complète trouvée — on peut arrêter
            break;
        }

        // Élaguer le front : grille 0.5° lat/lon, garder le meilleur par cellule
        front = prune_front(new_candidates);
        isochrones_count += 1;
    }

    let computation_ms = t_start.elapsed().as_millis() as u64;

    // Construire la route optimisée
    let (opt_eta, opt_waypoints) = if let Some(finish) = best_finish {
        let eta = finish.elapsed_h;
        let wps = build_waypoints(&finish.path, departure_epoch, time_step);
        (eta, wps)
    } else {
        // Pas de route trouvée — retourner la route directe
        let wps = vec![
            RouteWaypoint {
                lat: inp.start.lat,
                lon: inp.start.lon,
                time: inp.departure_time.clone(),
                speed_kts: speed_fallback,
                bearing_deg: direct_bearing,
            },
            RouteWaypoint {
                lat: end_lat,
                lon: end_lon,
                time: format_time(departure_epoch + (direct_eta * 3600.0) as i64),
                speed_kts: speed_fallback,
                bearing_deg: direct_bearing,
            },
        ];
        (direct_eta, wps)
    };

    let opt_dist: f64 = opt_waypoints.windows(2)
        .map(|w| haversine_nm(w[0].lat, w[0].lon, w[1].lat, w[1].lon))
        .sum();

    let gain_hours = (direct_eta - opt_eta).max(0.0);
    let gain_percent = if direct_eta > 0.0 {
        gain_hours / direct_eta * 100.0
    } else {
        0.0
    };

    Ok(OptimizeOutput {
        status: "success".to_string(),
        direct_route: DirectRoute {
            distance_nm: round1(direct_dist),
            eta_hours: round1(direct_eta),
        },
        optimized_route: OptimizedRoute {
            distance_nm: round1(opt_dist),
            eta_hours: round1(opt_eta),
            waypoints: opt_waypoints,
        },
        gain_hours: round1(gain_hours),
        gain_percent: round1(gain_percent),
        computation_time_ms: computation_ms,
        isochrones_count,
        points_evaluated,
    })
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn get_wind_at(grid: &[WindGridSlot], lat: f64, lon: f64, epoch: i64) -> Option<WindPoint> {
    if grid.is_empty() {
        return None;
    }

    // Trouver le slot de temps le plus proche
    let best_slot = grid.iter().min_by_key(|slot| {
        let t = parse_iso(&slot.time).unwrap_or(0);
        (t - epoch).abs()
    })?;

    // Trouver le point spatial le plus proche
    best_slot.points.iter().min_by(|a, b| {
        let da = (a.lat - lat).powi(2) + (a.lon - lon).powi(2);
        let db = (b.lat - lat).powi(2) + (b.lon - lon).powi(2);
        da.partial_cmp(&db).unwrap()
    }).cloned()
}

fn calc_speed(
    wind: &Option<WindPoint>,
    bearing: f64,
    polar: Option<&PolarTable>,
    speed_fallback: f64,
    limits: &BoatLimits,
) -> (f64, bool) {
    let (tws, twd, curr_speed, curr_dir) = if let Some(w) = wind {
        (w.wind_speed_kts, w.wind_dir_deg, w.current_speed_kts, w.current_dir_deg)
    } else {
        (speed_fallback * 2.0, bearing, None, None)
    };

    // Vérifier les limites de vent
    if tws > limits.wind_max_kts * 1.1 {
        return (0.0, false);
    }

    let boat_speed = if let Some(p) = polar {
        let twa = calc_twa(twd, bearing);
        let spd = p.get_speed(twa, tws);
        if spd < 0.5 { speed_fallback } else { spd }
    } else {
        speed_fallback
    };

    // Ajouter composante courant
    let current_comp = if let (Some(cs), Some(cd)) = (curr_speed, curr_dir) {
        current_component(cs, cd, bearing)
    } else {
        0.0
    };

    let effective = (boat_speed + current_comp).max(0.1);
    (effective, true)
}

/// Distance perpendiculaire d'un point à la ligne directe départ-arrivée
fn dist_from_direct_line(
    lat: f64, lon: f64,
    start_lat: f64, start_lon: f64,
    end_lat: f64, end_lon: f64,
) -> f64 {
    // Approximation simplifiée : distance haversine au midpoint de la route directe
    let mid_lat = (start_lat + end_lat) / 2.0;
    let mid_lon = (start_lon + end_lon) / 2.0;
    let route_len = haversine_nm(start_lat, start_lon, end_lat, end_lon);
    let to_mid = haversine_nm(lat, lon, mid_lat, mid_lon);
    // Approximation grossière mais rapide
    (to_mid - route_len / 2.0).abs()
}

/// Élagage du front : garder le meilleur point par cellule de 0.5°×0.5°
fn prune_front(candidates: Vec<FrontPoint>) -> Vec<FrontPoint> {
    use std::collections::HashMap;
    let cell_size = 0.5_f64;
    let mut best_by_cell: HashMap<(i32, i32), FrontPoint> = HashMap::new();

    for pt in candidates {
        let cell_lat = (pt.lat / cell_size).floor() as i32;
        let cell_lon = (pt.lon / cell_size).floor() as i32;
        let key = (cell_lat, cell_lon);
        let entry = best_by_cell.entry(key).or_insert_with(|| pt.clone());
        if pt.dist_to_end < entry.dist_to_end {
            *entry = pt;
        }
    }

    let mut result: Vec<FrontPoint> = best_by_cell.into_values().collect();
    // Limiter la taille du front pour les performances
    if result.len() > 500 {
        result.sort_by(|a, b| a.dist_to_end.partial_cmp(&b.dist_to_end).unwrap());
        result.truncate(500);
    }
    result
}

fn estimate_direct_eta(
    start_lat: f64, start_lon: f64,
    end_lat: f64, end_lon: f64,
    grid: &[WindGridSlot],
    departure_epoch: i64,
    polar: Option<&PolarTable>,
    speed_fallback: f64,
    limits: &BoatLimits,
) -> f64 {
    let dist = haversine_nm(start_lat, start_lon, end_lat, end_lon);
    let brng = bearing_deg(start_lat, start_lon, end_lat, end_lon);
    let wind = get_wind_at(grid, start_lat, start_lon, departure_epoch);
    let (speed, _) = calc_speed(&wind, brng, polar, speed_fallback, limits);
    if speed > 0.1 { dist / speed } else { dist / speed_fallback }
}

fn build_waypoints(
    path: &[(f64, f64)],
    departure_epoch: i64,
    time_step_h: f64,
) -> Vec<RouteWaypoint> {
    path.windows(2)
        .enumerate()
        .map(|(i, w)| {
            let epoch = departure_epoch + (i as f64 * time_step_h * 3600.0) as i64;
            let brng = bearing_deg(w[0].0, w[0].1, w[1].0, w[1].1);
            let dist = haversine_nm(w[0].0, w[0].1, w[1].0, w[1].1);
            RouteWaypoint {
                lat: w[0].0,
                lon: w[0].1,
                time: format_time(epoch),
                speed_kts: round1(dist / time_step_h),
                bearing_deg: round1(brng),
            }
        })
        .chain(std::iter::once({
            let last = path.last().unwrap();
            let epoch = departure_epoch + (path.len() as f64 * time_step_h * 3600.0) as i64;
            RouteWaypoint {
                lat: last.0,
                lon: last.1,
                time: format_time(epoch),
                speed_kts: 0.0,
                bearing_deg: 0.0,
            }
        }))
        .collect()
}

fn format_time(epoch: i64) -> String {
    use chrono::{DateTime, Utc};
    let dt = DateTime::<Utc>::from_timestamp(epoch, 0)
        .unwrap_or(DateTime::<Utc>::from_timestamp(0, 0).unwrap());
    dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}
