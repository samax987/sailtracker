use serde::{Deserialize, Serialize};

/// Point géographique simple
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GeoPoint {
    pub lat: f64,
    pub lon: f64,
}

/// Waypoint nommé
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Waypoint {
    pub lat: f64,
    pub lon: f64,
    pub name: Option<String>,
}

/// Conditions météo à un point donné
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WeatherCondition {
    pub wind_speed_kts: f64,
    pub wind_direction_deg: f64,
    pub wind_gusts_kts: Option<f64>,
    pub wave_height_m: Option<f64>,
    pub wave_period_s: Option<f64>,
    pub wave_direction_deg: Option<f64>,
    pub swell_height_m: Option<f64>,
    pub swell_period_s: Option<f64>,
    pub swell_direction_deg: Option<f64>,
    pub current_speed_kts: Option<f64>,
    /// Direction vers laquelle le courant va (convention océanographique)
    pub current_direction_deg: Option<f64>,
}

/// Prévision en un waypoint à un instant donné
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ForecastPoint {
    pub waypoint_index: usize,
    pub forecast_time: String,
    pub conditions: WeatherCondition,
}

/// Limites de sécurité et de confort du bateau
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BoatLimits {
    pub wind_max_kts: f64,
    pub wave_max_m: f64,
    pub swell_max_m: f64,
    pub ideal_wind_min_kts: f64,
    pub ideal_wind_max_kts: f64,
}

impl Default for BoatLimits {
    fn default() -> Self {
        Self {
            wind_max_kts: 30.0,
            wave_max_m: 3.0,
            swell_max_m: 3.5,
            ideal_wind_min_kts: 10.0,
            ideal_wind_max_kts: 20.0,
        }
    }
}

/// Résultat détaillé pour un waypoint dans une simulation de route
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WaypointResult {
    pub index: usize,
    pub name: Option<String>,
    pub lat: f64,
    pub lon: f64,
    pub distance_from_start_nm: f64,
    pub bearing_deg: f64,
    pub boat_speed_kts: f64,
    pub twa_deg: f64,
    pub tws_kts: f64,
    pub current_effect_kts: f64,
    pub effective_speed_kts: f64,
    pub eta_hours: f64,
    pub comfort_score: f64,
}

/// Alerte sur la route
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Alert {
    pub alert_type: String,
    pub message: String,
    pub severity: String,
}

/// Résultat de simulation pour une date de départ
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DepartureResult {
    pub departure_date: String,
    pub confidence_score: f64,
    pub comfort_score: f64,
    pub overall_score: f64,
    pub adjusted_eta_hours: f64,
    pub avg_wind_kts: f64,
    pub max_wind_kts: f64,
    pub avg_wave_m: f64,
    pub avg_current_effect_kts: f64,
    pub avg_polar_speed_kts: f64,
    pub used_polars: bool,
    pub alerts: Vec<String>,
    pub verdict: String,
}
