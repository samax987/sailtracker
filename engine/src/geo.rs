use std::f64::consts::PI;

pub const EARTH_RADIUS_NM: f64 = 3440.065;
const DEG_TO_RAD: f64 = PI / 180.0;
const RAD_TO_DEG: f64 = 180.0 / PI;

/// Distance haversine entre deux points en milles nautiques
pub fn haversine_nm(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let dlat = (lat2 - lat1) * DEG_TO_RAD;
    let dlon = (lon2 - lon1) * DEG_TO_RAD;
    let a = (dlat / 2.0).sin().powi(2)
        + (lat1 * DEG_TO_RAD).cos() * (lat2 * DEG_TO_RAD).cos() * (dlon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().asin();
    EARTH_RADIUS_NM * c
}

/// Cap (bearing) de point1 → point2 en degrés [0, 360)
pub fn bearing_deg(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let lat1r = lat1 * DEG_TO_RAD;
    let lat2r = lat2 * DEG_TO_RAD;
    let dlon = (lon2 - lon1) * DEG_TO_RAD;
    let y = dlon.sin() * lat2r.cos();
    let x = lat1r.cos() * lat2r.sin() - lat1r.sin() * lat2r.cos() * dlon.cos();
    normalize_angle(y.atan2(x) * RAD_TO_DEG)
}

/// Destination depuis (lat, lon) suivant bearing_deg sur distance_nm
pub fn destination_point(lat: f64, lon: f64, brng_deg: f64, distance_nm: f64) -> (f64, f64) {
    let latr = lat * DEG_TO_RAD;
    let lonr = lon * DEG_TO_RAD;
    let brng = brng_deg * DEG_TO_RAD;
    let d = distance_nm / EARTH_RADIUS_NM;

    let lat2 = (latr.sin() * d.cos() + latr.cos() * d.sin() * brng.cos()).asin();
    let lon2 =
        lonr + (brng.sin() * d.sin() * latr.cos()).atan2(d.cos() - latr.sin() * lat2.sin());

    (lat2 * RAD_TO_DEG, lon2 * RAD_TO_DEG)
}

/// True Wind Angle entre la direction du vent et le cap du bateau
/// wind_dir = direction D'OÙ vient le vent (convention météo)
/// Retourne 0–180 (0 = face au vent, 180 = vent arrière)
pub fn calc_twa(wind_dir_deg: f64, boat_heading_deg: f64) -> f64 {
    let mut angle = (wind_dir_deg - boat_heading_deg).abs() % 360.0;
    if angle > 180.0 {
        angle = 360.0 - angle;
    }
    angle
}

/// Composante du courant dans l'axe du cap (positive = favorable)
/// current_dir = direction VERS laquelle le courant va
pub fn current_component(current_speed: f64, current_dir_deg: f64, boat_heading_deg: f64) -> f64 {
    let angle = (current_dir_deg - boat_heading_deg) * DEG_TO_RAD;
    current_speed * angle.cos()
}

/// Normalise un angle en [0, 360)
pub fn normalize_angle(angle: f64) -> f64 {
    ((angle % 360.0) + 360.0) % 360.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_haversine() {
        // Mindelo → Bridgetown ≈ 2030 NM
        let d = haversine_nm(16.88, -25.00, 13.07, -59.62);
        assert!((d - 1979.0).abs() < 50.0, "haversine: {}", d);
    }

    #[test]
    fn test_bearing() {
        // Mindelo → Bridgetown ≈ 267° (cap ouest légèrement sud)
        let b = bearing_deg(16.88, -25.00, 13.07, -59.62);
        assert!((b - 267.0).abs() < 5.0, "bearing: {}", b);
    }

    #[test]
    fn test_twa_face() {
        // Vent de 0° (nord), cap 0° → TWA = 0 (face au vent)
        assert_eq!(calc_twa(0.0, 0.0), 0.0);
    }

    #[test]
    fn test_twa_arriere() {
        // Vent de 180° (sud), cap 0° → TWA = 180 (vent arrière)
        assert_eq!(calc_twa(180.0, 0.0), 180.0);
    }

    #[test]
    fn test_twa_travers() {
        // Vent de 90° (est), cap 0° → TWA = 90 (travers)
        assert_eq!(calc_twa(90.0, 0.0), 90.0);
    }

    #[test]
    fn test_destination() {
        // Partir de (0, 0), cap nord, 60 NM → lat ≈ 1°
        let (lat, lon) = destination_point(0.0, 0.0, 0.0, 60.0);
        assert!((lat - 1.0).abs() < 0.05, "lat: {}", lat);
        assert!(lon.abs() < 0.001, "lon: {}", lon);
    }
}
