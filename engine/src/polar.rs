use crate::geo::calc_twa;
use csv::ReaderBuilder;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json;
use std::f64::consts::PI;

/// Table de polaires chargée depuis CSV (séparateur `;`)
/// Format :
///   TWA/TWS;4;6;8;10;12;14;16;18;20;25;30;35;40
///   0;0.0;0.0;...
///   30;1.2;1.8;...
pub struct PolarTable {
    pub twa_values: Vec<f64>,
    pub tws_values: Vec<f64>,
    /// speeds[twa_idx][tws_idx] = vitesse bateau en nœuds
    pub speeds: Vec<Vec<f64>>,
}

impl PolarTable {
    pub fn from_csv(path: &str) -> Result<Self, String> {
        let mut rdr = ReaderBuilder::new()
            .delimiter(b';')
            .has_headers(true)
            .from_path(path)
            .map_err(|e| format!("Impossible d'ouvrir le CSV '{}': {}", path, e))?;

        let headers = rdr
            .headers()
            .map_err(|e| format!("Erreur lecture entête CSV: {}", e))?
            .clone();

        let tws_values: Vec<f64> = headers
            .iter()
            .skip(1) // ignorer "TWA/TWS"
            .map(|s| {
                s.trim()
                    .parse::<f64>()
                    .map_err(|e| format!("Valeur TWS invalide '{}': {}", s, e))
            })
            .collect::<Result<Vec<_>, _>>()?;

        let mut twa_values = Vec::new();
        let mut speeds = Vec::new();

        for result in rdr.records() {
            let record =
                result.map_err(|e| format!("Erreur lecture enregistrement CSV: {}", e))?;
            let mut fields = record.iter();

            let twa_str = fields.next().ok_or("Champ TWA manquant")?;
            let twa: f64 = twa_str
                .trim()
                .parse()
                .map_err(|e| format!("TWA invalide '{}': {}", twa_str, e))?;

            let row: Vec<f64> = fields
                .map(|s| s.trim().parse::<f64>().unwrap_or(0.0))
                .collect();

            twa_values.push(twa);
            speeds.push(row);
        }

        if twa_values.is_empty() {
            return Err("CSV de polaires vide".to_string());
        }

        Ok(Self {
            twa_values,
            tws_values,
            speeds,
        })
    }


    pub fn from_sqlite(db_path: &str) -> Result<Self, String> {
        let conn = Connection::open(db_path)
            .map_err(|e| format!("DB open error: {}", e))?;

        let mut stmt = conn
            .prepare("SELECT twa_deg, tws_kts, speed_kts FROM polar_matrix ORDER BY twa_deg, tws_kts")
            .map_err(|e| format!("Prepare error: {}", e))?;

        let rows_data: Vec<(f64, f64, f64)> = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .map_err(|e| format!("Query error: {}", e))?
            .filter_map(|r| r.ok())
            .collect();

        if rows_data.is_empty() {
            return Err("polar_matrix est vide".to_string());
        }

        let mut twa_set: Vec<f64> = rows_data.iter().map(|r| r.0).collect();
        twa_set.dedup();
        let mut tws_set: Vec<f64> = rows_data.iter().map(|r| r.1).collect();
        tws_set.sort_by(|a, b| a.partial_cmp(b).unwrap());
        tws_set.dedup();

        let mut speeds = vec![vec![0.0f64; tws_set.len()]; twa_set.len()];
        for (twa, tws, spd) in &rows_data {
            if let (Some(ti), Some(wi)) = (
                twa_set.iter().position(|v| (v - twa).abs() < 0.01),
                tws_set.iter().position(|v| (v - tws).abs() < 0.01),
            ) {
                speeds[ti][wi] = *spd;
            }
        }

        Ok(PolarTable { twa_values: twa_set, tws_values: tws_set, speeds })
    }

    /// Auto-detect: .db → SQLite, sinon → CSV
    pub fn load(path: &str) -> Result<Self, String> {
        if path.ends_with(".db") {
            Self::from_sqlite(path)
        } else {
            Self::from_csv(path)
        }
    }

    /// Interpolation bilinéaire TWA × TWS → vitesse bateau (nœuds)
    pub fn get_speed(&self, twa: f64, tws: f64) -> f64 {
        let twa = twa.clamp(self.twa_values[0], *self.twa_values.last().unwrap());
        let tws = tws.clamp(self.tws_values[0], *self.tws_values.last().unwrap());

        let (ti0, ti1) = find_bracket(&self.twa_values, twa);
        let (si0, si1) = find_bracket(&self.tws_values, tws);

        let twa_frac = interp_frac(self.twa_values[ti0], self.twa_values[ti1], twa);
        let tws_frac = interp_frac(self.tws_values[si0], self.tws_values[si1], tws);

        let v00 = self.speeds[ti0].get(si0).copied().unwrap_or(0.0);
        let v01 = self.speeds[ti0].get(si1).copied().unwrap_or(0.0);
        let v10 = self.speeds[ti1].get(si0).copied().unwrap_or(0.0);
        let v11 = self.speeds[ti1].get(si1).copied().unwrap_or(0.0);

        let v0 = v00 + (v01 - v00) * tws_frac;
        let v1 = v10 + (v11 - v10) * tws_frac;
        v0 + (v1 - v0) * twa_frac
    }

    /// VMG (Velocity Made Good) = composante de la vitesse dans l'axe du vent
    pub fn get_vmg(&self, twa: f64, tws: f64) -> f64 {
        let speed = self.get_speed(twa, tws);
        speed * (twa * PI / 180.0).cos()
    }
}

fn find_bracket(values: &[f64], target: f64) -> (usize, usize) {
    for i in 0..values.len().saturating_sub(1) {
        if target >= values[i] && target <= values[i + 1] {
            return (i, i + 1);
        }
    }
    let last = values.len() - 1;
    (last, last)
}

fn interp_frac(lo: f64, hi: f64, val: f64) -> f64 {
    if (hi - lo).abs() < 1e-9 {
        0.0
    } else {
        (val - lo) / (hi - lo)
    }
}

// ─── Commande CLI ─────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct PolarInput {
    twa: f64,
    tws: f64,
}

#[derive(Serialize)]
struct PolarOutput {
    speed_kts: f64,
    vmg_kts: f64,
}

pub fn run(input: String, polars_path: &str) -> Result<String, String> {
    let inp: PolarInput = serde_json::from_str(&input)
        .map_err(|e| format!("JSON invalide pour la commande polar: {}", e))?;

    let table = PolarTable::load(polars_path)?;

    let speed = table.get_speed(inp.twa, inp.tws);
    let vmg = table.get_vmg(inp.twa, inp.tws);

    let out = PolarOutput {
        speed_kts: (speed * 100.0).round() / 100.0,
        vmg_kts: (vmg * 100.0).round() / 100.0,
    };

    serde_json::to_string(&out).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load_test_polars() -> PolarTable {
        PolarTable::from_csv("../data/polars/pollen1.csv")
            .expect("Fichier de polaires introuvable")
    }

    #[test]
    fn test_polar_face_au_vent() {
        let p = load_test_polars();
        // TWA=0 → face au vent → vitesse = 0
        let s = p.get_speed(0.0, 15.0);
        assert_eq!(s, 0.0, "Face au vent: {}", s);
    }

    #[test]
    fn test_polar_travers() {
        let p = load_test_polars();
        // TWA=90, TWS=16 → d'après le CSV : 7.6 kts
        let s = p.get_speed(90.0, 16.0);
        assert!((s - 7.6).abs() < 0.2, "Travers 90°/16kts: {}", s);
    }

    #[test]
    fn test_polar_interpolation() {
        let p = load_test_polars();
        // TWA=87.5 (entre 85 et 90), TWS=16 → entre 7.7 et 7.6 ≈ 7.65
        let s = p.get_speed(87.5, 16.0);
        assert!((s - 7.65).abs() < 0.1, "Interpolé 87.5°/16kts: {}", s);
    }

    #[test]
    fn test_polar_vent_arriere() {
        let p = load_test_polars();
        // TWA=180, TWS=20 → 2.6 kts
        let s = p.get_speed(180.0, 20.0);
        assert!((s - 2.6).abs() < 0.1, "Vent arrière 180°/20kts: {}", s);
    }
}
