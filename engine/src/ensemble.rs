use serde::{Deserialize, Serialize};
use serde_json;

// ─── Structures d'entrée/sortie ───────────────────────────────────────────────

#[derive(Deserialize)]
struct EnsembleInput {
    members: Vec<f64>,
}

#[derive(Serialize)]
struct EnsembleOutput {
    count: usize,
    mean: f64,
    std: f64,
    min: f64,
    max: f64,
    p10: f64,
    p25: f64,
    p50: f64,
    p75: f64,
    p90: f64,
}

// ─── Commande CLI ─────────────────────────────────────────────────────────────

pub fn run(input: String) -> Result<String, String> {
    let inp: EnsembleInput = serde_json::from_str(&input)
        .map_err(|e| format!("JSON invalide pour la commande ensemble: {}", e))?;

    if inp.members.is_empty() {
        return Err("Liste de membres vide".to_string());
    }

    let out = compute_stats(&inp.members);
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

// ─── Calcul des statistiques ──────────────────────────────────────────────────

pub fn compute_stats(members: &[f64]) -> EnsembleOutput {
    let n = members.len();

    // Moyenne
    let mean = members.iter().sum::<f64>() / n as f64;

    // Écart-type (population)
    let variance = members.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n as f64;
    let std = variance.sqrt();

    // Tri pour percentiles
    let mut sorted = members.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let min = sorted[0];
    let max = sorted[n - 1];

    EnsembleOutput {
        count: n,
        mean: round2(mean),
        std: round2(std),
        min: round2(min),
        max: round2(max),
        p10: round2(percentile(&sorted, 10.0)),
        p25: round2(percentile(&sorted, 25.0)),
        p50: round2(percentile(&sorted, 50.0)),
        p75: round2(percentile(&sorted, 75.0)),
        p90: round2(percentile(&sorted, 90.0)),
    }
}

/// Percentile par interpolation linéaire (méthode R-7, même que numpy)
fn percentile(sorted: &[f64], p: f64) -> f64 {
    let n = sorted.len();
    if n == 1 {
        return sorted[0];
    }

    let h = (p / 100.0) * (n as f64 - 1.0);
    let lo = h.floor() as usize;
    let hi = h.ceil() as usize;

    if lo == hi {
        sorted[lo]
    } else {
        sorted[lo] + (sorted[hi] - sorted[lo]) * (h - lo as f64)
    }
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ensemble_basic() {
        let members = vec![14.2, 15.1, 13.8, 16.0, 15.5];
        let s = compute_stats(&members);
        assert!((s.mean - 14.92).abs() < 0.05, "mean: {}", s.mean);
        assert!(s.min < 14.0);
        assert!(s.max > 15.9);
    }

    #[test]
    fn test_ensemble_single() {
        let members = vec![7.0];
        let s = compute_stats(&members);
        assert_eq!(s.mean, 7.0);
        assert_eq!(s.std, 0.0);
        assert_eq!(s.p50, 7.0);
    }

    #[test]
    fn test_percentile_symmetric() {
        let members = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let s = compute_stats(&members);
        assert!((s.p50 - 3.0).abs() < 0.01, "p50: {}", s.p50);
        assert!((s.p10 - 1.4).abs() < 0.1, "p10: {}", s.p10);
        assert!((s.p90 - 4.6).abs() < 0.1, "p90: {}", s.p90);
    }

    #[test]
    fn test_std_known() {
        // [1, 3, 5, 7, 9] → mean=5, var=8, std=2.83
        let members = vec![1.0, 3.0, 5.0, 7.0, 9.0];
        let s = compute_stats(&members);
        assert!((s.mean - 5.0).abs() < 0.01);
        assert!((s.std - 2.83).abs() < 0.01, "std: {}", s.std);
    }
}
