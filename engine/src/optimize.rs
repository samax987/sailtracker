use crate::geo::{bearing_deg, calc_twa, current_component, destination_point, haversine_nm,
                 normalize_angle};
use crate::polar::PolarTable;
use crate::route::parse_iso;
use crate::types::BoatLimits;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json;
use std::collections::HashMap;
use std::time::Instant;

// ─── Structures I/O ───────────────────────────────────────────────────────────

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

// ─── Détection des terres ────────────────────────────────────────────────────
// Implémentation : voir landmask.rs (Natural Earth 10m + R-tree + point-in-polygon).

use crate::landmask::{crosses_land_buffered, is_on_land};

// ─── Structures internes ──────────────────────────────────────────────────────

/// Point archivé pour le backtracking (minimal)
#[derive(Clone)]
struct HistPoint {
    lat: f64,
    lon: f64,
    parent_gen: i32,   // -1 = point de départ (racine)
    parent_idx: usize, // index dans history[parent_gen]
}

/// Point du front actif (données de travail)
#[derive(Clone)]
struct FrontPoint {
    lat: f64,
    lon: f64,
    elapsed_h: f64,    // heures depuis le départ
    dist_to_end: f64,  // distance restante jusqu'à la destination (nm)
    parent_gen: i32,
    parent_idx: usize,
}

// ─── API publique ─────────────────────────────────────────────────────────────

pub fn run(input: String, polars_path: &str) -> Result<String, String> {
    let inp: OptimizeInput = serde_json::from_str(&input)
        .map_err(|e| format!("JSON invalide pour optimize: {}", e))?;
    let polar = PolarTable::load(polars_path).ok();
    let result = isochrone_optimize(&inp, polar.as_ref())?;
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

// ─── Algorithme isochrone (réécriture complète) ───────────────────────────────

pub fn isochrone_optimize(
    inp: &OptimizeInput,
    polar: Option<&PolarTable>,
) -> Result<OptimizeOutput, String> {
    let t_start = Instant::now();

    let departure_epoch = parse_iso(&inp.departure_time)
        .ok_or("Impossible de parser departure_time")?;

    let angle_step = inp.angle_step_deg.unwrap_or(5.0).max(1.0) as u32;
    let time_step = inp.time_step_hours.unwrap_or(1.0);
    let limits = inp.limits.clone().unwrap_or_default();
    let speed_fallback = inp.boat_speed_fixed_kts.unwrap_or(6.0);

    let end_lat = inp.end.lat;
    let end_lon = inp.end.lon;

    let direct_dist = haversine_nm(inp.start.lat, inp.start.lon, end_lat, end_lon);
    let direct_eta = estimate_direct_eta(
        inp.start.lat, inp.start.lon, end_lat, end_lon,
        &inp.wind_grid, departure_epoch, polar, speed_fallback, &limits,
    );

    // ÉTAPE 1 — Initialisation
    // Valeurs TWA : 0°, 5°, …, 180°
    let twa_values: Vec<f64> = (0u32..=180)
        .step_by(angle_step as usize)
        .map(|t| t as f64)
        .collect();

    let start_fp = FrontPoint {
        lat: inp.start.lat,
        lon: inp.start.lon,
        elapsed_h: 0.0,
        dist_to_end: direct_dist,
        parent_gen: -1,
        parent_idx: 0,
    };

    // Historique des fronts archivés (pour backtracking par indices de parenté)
    let mut history: Vec<Vec<HistPoint>> = Vec::new();
    let mut front: Vec<FrontPoint> = vec![start_fp];
    let mut best_finish: Option<FrontPoint> = None;
    let mut isochrones_count = 0usize;
    let mut points_evaluated = 0u64;

    // Max 30 jours
    let max_steps = ((30.0 * 24.0) / time_step) as usize;
    // Rayon d'arrivée adaptatif : 20% de la distance directe, borné [2.0, 50.0] NM.
    // Évite que des routes courtes ne s'arrêtent sur le 1er saut, sans pour autant
    // surcontraindre les routes locales où trop d'isos rendent le snap final impossible.
    let arrival_radius_nm = (direct_dist * 0.20).clamp(2.0, 50.0);
    // Buffer côtier : ignorer les `coastal_buffer_nm` derniers/premiers NM du segment
    // de snap. Permet l'arrivée vers un mouillage proche d'une île (endpoint peut être
    // marqué "on land" selon la résolution Natural Earth 10m).
    let coastal_buffer_nm: f64 = 0.5;
    let max_front_size = 1000usize;

    // ÉTAPE 2 — Propagation
    for _step in 0..max_steps {
        if front.is_empty() {
            break;
        }

        // Archiver le front courant dans l'historique (indice = gen_id)
        let gen_id = history.len() as i32;
        history.push(
            front
                .iter()
                .map(|fp| HistPoint {
                    lat: fp.lat,
                    lon: fp.lon,
                    parent_gen: fp.parent_gen,
                    parent_idx: fp.parent_idx,
                })
                .collect(),
        );

        // Tous les points du front sont à la même heure (propriété des isochrones)
        let elapsed_h = front[0].elapsed_h;
        let current_epoch = departure_epoch + (elapsed_h * 3600.0) as i64;

        // Expansion parallèle : pour chaque point × chaque TWA × chaque bord
        let candidates: Vec<FrontPoint> = front
            .par_iter()
            .enumerate()
            .flat_map(|(i, fp)| {
                // Vent au point courant à l'heure courante (calculé une seule fois par point)
                let wind = get_wind_at(&inp.wind_grid, fp.lat, fp.lon, current_epoch);
                let (twd, tws) = match &wind {
                    Some(w) => (w.wind_dir_deg, w.wind_speed_kts),
                    None => {
                        // Pas de données vent : cap direct, vent fictif
                        (
                            bearing_deg(fp.lat, fp.lon, end_lat, end_lon),
                            speed_fallback * 1.5,
                        )
                    }
                };

                // Limite de vent max
                if tws > limits.wind_max_kts * 1.1 {
                    return vec![];
                }

                let mut result: Vec<FrontPoint> = Vec::new();

                for &twa in &twa_values {
                    // Vitesse polaire pour ce TWA
                    let polar_speed = match polar {
                        Some(p) => p.get_speed(twa, tws),
                        None => speed_fallback,
                    };
                    if polar_speed < 0.5 {
                        continue; // allure non navigable
                    }

                    // Nombre de bords : 1 seul pour TWA=0° (nez au vent) et TWA=180° (vent arrière)
                    let n_tacks: usize = if twa == 0.0 || twa == 180.0 { 1 } else { 2 };

                    for k in 0..n_tacks {
                        // sign=-1 → tribord (vent à droite), sign=+1 → bâbord (vent à gauche)
                        let sign = if k == 0 { -1.0_f64 } else { 1.0_f64 };
                        let hdg = normalize_angle(twd + sign * twa);

                        // Composante du courant dans l'axe du cap
                        let current_comp = wind
                            .as_ref()
                            .and_then(|w| {
                                Some(current_component(
                                    w.current_speed_kts?,
                                    w.current_dir_deg?,
                                    hdg,
                                ))
                            })
                            .unwrap_or(0.0);

                        let effective_speed = (polar_speed + current_comp).max(0.1);
                        let dist_moved = effective_speed * time_step;

                        let (new_lat, new_lon) =
                            destination_point(fp.lat, fp.lon, hdg, dist_moved);

                        // Filtres : terre, limites géographiques
                        // Note : on ne teste que le point d'arrivée du segment (pas le segment entier)
                        // pour des raisons de perf. Avec un step <= 0.25h et ~5kt, le segment fait <2 NM
                        // et le risque qu'il traverse une côte sans que le point arrivée soit terrestre
                        // est faible. Le check segment complet n'est utilisé que pour le snap final.
                        if is_on_land(new_lat, new_lon) {
                            continue;
                        }
                        if !(-90.0..=90.0).contains(&new_lat)
                            || !(-180.0..=180.0).contains(&new_lon)
                        {
                            continue;
                        }

                        let dist_to_end =
                            haversine_nm(new_lat, new_lon, end_lat, end_lon);

                        result.push(FrontPoint {
                            lat: new_lat,
                            lon: new_lon,
                            elapsed_h: elapsed_h + time_step,
                            dist_to_end,
                            parent_gen: gen_id,
                            parent_idx: i,
                        });
                    }
                }
                result
            })
            .collect();

        points_evaluated += candidates.len() as u64;

        // Vérifier si un candidat est dans le rayon d'arrivée
        let arrivals: Vec<&FrontPoint> = candidates
            .iter()
            .filter(|pt| pt.dist_to_end < arrival_radius_nm)
            .collect();

        // Filtrer les arrivées dont le snap final (candidat -> end) traverse la terre,
        // sinon le dernier segment risque de traverser une île.
        // On exempte un buffer côtier autour de end (mouillage proche d'une île).
        let valid_arrivals: Vec<&FrontPoint> = arrivals
            .iter()
            .filter(|pt| {
                !crosses_land_buffered(pt.lat, pt.lon, end_lat, end_lon, coastal_buffer_nm)
            })
            .copied()
            .collect();

        if !valid_arrivals.is_empty() {
            // Meilleur candidat d'arrivée = celui avec le temps total le plus court
            let best_arr = valid_arrivals
                .iter()
                .min_by(|a, b| {
                    let snap_speed = speed_fallback.max(0.5);
                    let ta = a.elapsed_h + a.dist_to_end / snap_speed;
                    let tb = b.elapsed_h + b.dist_to_end / snap_speed;
                    ta.partial_cmp(&tb).unwrap()
                })
                .unwrap();

            // Calculer le temps du segment final vers la destination exacte
            let snap_bearing = bearing_deg(best_arr.lat, best_arr.lon, end_lat, end_lon);
            let snap_wind = get_wind_at(
                &inp.wind_grid,
                best_arr.lat,
                best_arr.lon,
                current_epoch + (time_step * 3600.0) as i64,
            );
            let snap_speed = match polar {
                Some(p) => snap_wind
                    .as_ref()
                    .map(|w| {
                        let twa = calc_twa(w.wind_dir_deg, snap_bearing);
                        p.get_speed(twa, w.wind_speed_kts)
                    })
                    .unwrap_or(speed_fallback)
                    .max(0.5),
                None => speed_fallback,
            };
            let snap_time_h = best_arr.dist_to_end / snap_speed;

            // Archiver le candidat d'arrivée dans une génération dédiée
            let arrival_gen = history.len() as i32;
            history.push(vec![HistPoint {
                lat: best_arr.lat,
                lon: best_arr.lon,
                parent_gen: best_arr.parent_gen,
                parent_idx: best_arr.parent_idx,
            }]);

            // Point d'arrivée final = destination exacte
            best_finish = Some(FrontPoint {
                lat: end_lat,
                lon: end_lon,
                elapsed_h: best_arr.elapsed_h + snap_time_h,
                dist_to_end: 0.0,
                parent_gen: arrival_gen,
                parent_idx: 0,
            });
            break; // STOP
        }

        // Élaguer le front : grille 0.25°, garder le meilleur (min dist_to_end) par cellule
        front = prune_front(candidates, max_front_size);
        isochrones_count += 1;
    }

    let computation_ms = t_start.elapsed().as_millis() as u64;

    // ÉTAPE 3 — Backtrack et construction de la route
    let (opt_eta, opt_waypoints) = if let Some(ref finish) = best_finish {
        let path = backtrack_path(&history, finish);
        let eta = finish.elapsed_h;
        let wps = build_waypoints(&path, departure_epoch, time_step, eta);
        (eta, wps)
    } else {
        // Aucune route trouvée → route directe de secours
        let direct_bearing = bearing_deg(inp.start.lat, inp.start.lon, end_lat, end_lon);
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

    let opt_dist: f64 = opt_waypoints
        .windows(2)
        .map(|w| haversine_nm(w[0].lat, w[0].lon, w[1].lat, w[1].lon))
        .sum();

    let gain_hours = (direct_eta - opt_eta).max(0.0);
    let gain_percent = if direct_eta > 0.0 {
        gain_hours / direct_eta * 100.0
    } else {
        0.0
    };

    Ok(OptimizeOutput {
        status: if best_finish.is_some() {
            "success".to_string()
        } else {
            "fallback_direct".to_string()
        },
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

// ─── Élagage du front ─────────────────────────────────────────────────────────

/// Grille 0.25°×0.25° : garde le point avec la plus petite dist_to_end par cellule.
/// Limite le front à max_points points (les plus proches de la destination).
fn prune_front(candidates: Vec<FrontPoint>, max_points: usize) -> Vec<FrontPoint> {
    let cell_size = 0.25_f64;
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
    if result.len() > max_points {
        result.sort_unstable_by(|a, b| {
            a.dist_to_end.partial_cmp(&b.dist_to_end).unwrap()
        });
        result.truncate(max_points);
    }
    result
}

// ─── Backtracking ─────────────────────────────────────────────────────────────

/// Remonte le chemin depuis finish jusqu'au départ via les indices de parenté.
fn backtrack_path(history: &[Vec<HistPoint>], finish: &FrontPoint) -> Vec<(f64, f64)> {
    let mut path = vec![(finish.lat, finish.lon)];
    let mut gen = finish.parent_gen;
    let mut idx = finish.parent_idx;

    loop {
        if gen < 0 || (gen as usize) >= history.len() {
            break;
        }
        let gen_slice = &history[gen as usize];
        if idx >= gen_slice.len() {
            break;
        }
        let hp = &gen_slice[idx];
        path.push((hp.lat, hp.lon));
        if hp.parent_gen < 0 {
            break; // racine atteinte (point de départ)
        }
        gen = hp.parent_gen;
        idx = hp.parent_idx;
    }

    path.reverse();
    path
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn get_wind_at(grid: &[WindGridSlot], lat: f64, lon: f64, epoch: i64) -> Option<WindPoint> {
    if grid.is_empty() {
        return None;
    }
    // Slot temporel le plus proche
    let best_slot = grid.iter().min_by_key(|slot| {
        let t = parse_iso(&slot.time).unwrap_or(0);
        (t - epoch).abs()
    })?;
    // Point spatial le plus proche
    best_slot
        .points
        .iter()
        .min_by(|a, b| {
            let da = (a.lat - lat).powi(2) + (a.lon - lon).powi(2);
            let db = (b.lat - lat).powi(2) + (b.lon - lon).powi(2);
            da.partial_cmp(&db).unwrap()
        })
        .cloned()
}

fn estimate_direct_eta(
    start_lat: f64, start_lon: f64,
    end_lat: f64, end_lon: f64,
    grid: &[WindGridSlot],
    departure_epoch: i64,
    polar: Option<&PolarTable>,
    speed_fallback: f64,
    _limits: &BoatLimits,
) -> f64 {
    let dist = haversine_nm(start_lat, start_lon, end_lat, end_lon);
    let brng = bearing_deg(start_lat, start_lon, end_lat, end_lon);
    let wind = get_wind_at(grid, start_lat, start_lon, departure_epoch);
    let speed = match polar {
        Some(p) => wind
            .as_ref()
            .map(|w| {
                let twa = calc_twa(w.wind_dir_deg, brng);
                p.get_speed(twa, w.wind_speed_kts)
            })
            .unwrap_or(speed_fallback)
            .max(0.5),
        None => speed_fallback,
    };
    dist / speed
}

/// Construit la liste des waypoints depuis le chemin (lat, lon).
fn build_waypoints(
    path: &[(f64, f64)],
    departure_epoch: i64,
    time_step_h: f64,
    total_elapsed_h: f64,
) -> Vec<RouteWaypoint> {
    let n = path.len();
    if n == 0 {
        return vec![];
    }
    if n == 1 {
        return vec![RouteWaypoint {
            lat: path[0].0,
            lon: path[0].1,
            time: format_time(departure_epoch),
            speed_kts: 0.0,
            bearing_deg: 0.0,
        }];
    }

    path.windows(2)
        .enumerate()
        .map(|(i, w)| {
            let epoch = departure_epoch + (i as f64 * time_step_h * 3600.0) as i64;
            let brng = bearing_deg(w[0].0, w[0].1, w[1].0, w[1].1);
            let dist = haversine_nm(w[0].0, w[0].1, w[1].0, w[1].1);
            // Durée du segment : time_step_h sauf le dernier (durée réelle calculée)
            let seg_h = if i == n - 2 {
                (total_elapsed_h - i as f64 * time_step_h).max(0.01)
            } else {
                time_step_h
            };
            RouteWaypoint {
                lat: w[0].0,
                lon: w[0].1,
                time: format_time(epoch),
                speed_kts: round1(dist / seg_h),
                bearing_deg: round1(brng),
            }
        })
        .chain(std::iter::once({
            // Dernier waypoint = destination exacte, horodaté à l'ETA total
            let last = path.last().unwrap();
            RouteWaypoint {
                lat: last.0,
                lon: last.1,
                time: format_time(departure_epoch + (total_elapsed_h * 3600.0) as i64),
                speed_kts: 0.0,
                bearing_deg: 0.0,
            }
        }))
        .collect()
}

fn format_time(epoch: i64) -> String {
    use chrono::{DateTime, Utc};
    let dt = DateTime::<Utc>::from_timestamp(epoch, 0)
        .unwrap_or_else(|| DateTime::<Utc>::from_timestamp(0, 0).unwrap());
    dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}
