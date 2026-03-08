"""
Shared utility functions for SailTracker.
"""
import math


def haversine_nm(lat1, lon1, lat2, lon2):
    """Distance in nautical miles between two lat/lon points."""
    R = 3440.065  # Earth radius in NM
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def knots_to_beaufort(kt):
    """Convert wind speed in knots to Beaufort scale (0-12)."""
    thresholds = [1, 3, 6, 10, 16, 21, 27, 33, 40, 47, 55, 63]
    for i, t in enumerate(thresholds):
        if kt < t:
            return i
    return 12


def angular_error(a, b):
    """Angular error between two directions (0-360), accounting for wrap-around."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)
