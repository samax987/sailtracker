//! Détection terre/mer basée sur Natural Earth 10m (land + minor_islands).
//!
//! Au premier appel : parse le GeoJSON embarqué, construit un R-tree des bounding boxes
//! pour pré-filtrer rapidement, puis test point-in-polygon sur les candidats.

use geocrate::{BoundingRect, Contains, Coord, LineString, MultiPolygon, Point, Polygon};
use geojson::{GeoJson, Geometry, Value};
use once_cell::sync::Lazy;
use rstar::{RTree, RTreeObject, AABB};

const COASTLINE_GEOJSON: &str = include_str!("../data/coastline.geojson");

struct IndexedPoly {
    poly: MultiPolygon<f64>,
    bbox: AABB<[f64; 2]>,
}

impl RTreeObject for IndexedPoly {
    type Envelope = AABB<[f64; 2]>;
    fn envelope(&self) -> Self::Envelope {
        self.bbox
    }
}

fn ring_to_linestring(ring: Vec<Vec<f64>>) -> LineString<f64> {
    let coords: Vec<Coord<f64>> = ring
        .into_iter()
        .filter_map(|c| {
            if c.len() >= 2 {
                Some(Coord { x: c[0], y: c[1] })
            } else {
                None
            }
        })
        .collect();
    LineString::new(coords)
}

fn polygon_from_rings(rings: Vec<Vec<Vec<f64>>>) -> Option<Polygon<f64>> {
    let mut iter = rings.into_iter();
    let exterior = ring_to_linestring(iter.next()?);
    let interiors: Vec<LineString<f64>> = iter.map(ring_to_linestring).collect();
    Some(Polygon::new(exterior, interiors))
}

fn collect_polygons(geom: &Geometry, out: &mut Vec<MultiPolygon<f64>>) {
    match &geom.value {
        Value::Polygon(rings) => {
            if let Some(poly) = polygon_from_rings(rings.clone()) {
                out.push(MultiPolygon(vec![poly]));
            }
        }
        Value::MultiPolygon(mps) => {
            let polys: Vec<Polygon<f64>> = mps
                .iter()
                .filter_map(|r| polygon_from_rings(r.clone()))
                .collect();
            if !polys.is_empty() {
                out.push(MultiPolygon(polys));
            }
        }
        Value::GeometryCollection(gs) => {
            for g in gs {
                collect_polygons(g, out);
            }
        }
        _ => {}
    }
}

fn build_index() -> RTree<IndexedPoly> {
    let parsed: GeoJson = COASTLINE_GEOJSON
        .parse()
        .expect("coastline.geojson invalide");
    let fc = match parsed {
        GeoJson::FeatureCollection(fc) => fc,
        _ => panic!("coastline.geojson : FeatureCollection attendue"),
    };

    let mut polys: Vec<MultiPolygon<f64>> = Vec::new();
    for feat in fc.features {
        if let Some(geom) = feat.geometry {
            collect_polygons(&geom, &mut polys);
        }
    }

    let indexed: Vec<IndexedPoly> = polys
        .into_iter()
        .filter_map(|mp| {
            let r = mp.bounding_rect()?;
            let bbox = AABB::from_corners([r.min().x, r.min().y], [r.max().x, r.max().y]);
            Some(IndexedPoly { poly: mp, bbox })
        })
        .collect();

    RTree::bulk_load(indexed)
}

static INDEX: Lazy<RTree<IndexedPoly>> = Lazy::new(build_index);

/// True si le point (lat, lon) est sur une masse terrestre.
pub fn is_on_land(lat: f64, lon: f64) -> bool {
    let p = Point::new(lon, lat);
    let envelope = AABB::from_point([lon, lat]);
    INDEX
        .locate_in_envelope_intersecting(&envelope)
        .any(|ip| ip.poly.contains(&p))
}

/// Force la construction de l'index (utile pour mesurer ou pré-charger).
pub fn warm_up() {
    Lazy::force(&INDEX);
}

/// True si le segment lat1,lon1 → lat2,lon2 traverse une masse terrestre,
/// en ignorant un "buffer" de `buffer_nm` autour de chaque extrémité.
/// Le buffer permet le départ/arrivée depuis un mouillage côtier (endpoint
/// peut être lui-même on-land selon la résolution du dataset).
pub fn crosses_land_buffered(
    lat1: f64,
    lon1: f64,
    lat2: f64,
    lon2: f64,
    buffer_nm: f64,
) -> bool {
    let dlat = lat2 - lat1;
    let dlon = (lon2 - lon1) * lat1.to_radians().cos();
    let approx_nm = (dlat * dlat + dlon * dlon).sqrt() * 60.0;
    let usable_nm = approx_nm - 2.0 * buffer_nm;
    if usable_nm < 0.2 {
        return false;
    }
    let n = ((approx_nm * 2.0).ceil() as usize).clamp(2, 60);
    let t_buf = if approx_nm > 0.0 {
        buffer_nm / approx_nm
    } else {
        0.0
    };
    for i in 1..n {
        let t = i as f64 / n as f64;
        if t < t_buf || t > 1.0 - t_buf {
            continue;
        }
        let lat = lat1 + (lat2 - lat1) * t;
        let lon = lon1 + (lon2 - lon1) * t;
        if is_on_land(lat, lon) {
            return true;
        }
    }
    false
}

/// True si le segment lat1,lon1 → lat2,lon2 traverse une masse terrestre.
/// Wrapper sans buffer, conservé pour rétro-compat.
pub fn crosses_land(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> bool {
    crosses_land_buffered(lat1, lon1, lat2, lon2, 0.0)
}
