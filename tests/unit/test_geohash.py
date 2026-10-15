import random

import pytest

from transit import geohash


def test_known_vectors():
    # Reference values from the original geohash.org examples.
    assert geohash.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert geohash.encode(42.6, -5.6, 5) == "ezs42"


def test_bounds_contain_point():
    rnd = random.Random(1)
    for _ in range(500):
        lat, lon = rnd.uniform(1.2, 1.47), rnd.uniform(103.6, 104.05)
        for p in (5, 6, 7):
            lo_lat, hi_lat, lo_lon, hi_lon = geohash.bounds(geohash.encode(lat, lon, p))
            assert lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon


def test_neighbours_are_nine_adjacent_cells():
    gh = geohash.encode(1.2966, 103.8525, 6)
    cells = geohash.neighbours(gh)
    assert len(cells) == 9 and gh in cells
    lat_lo, lat_hi, lon_lo, lon_hi = geohash.bounds(gh)
    for c in cells:
        a, b, c2, d = geohash.bounds(c)
        # every neighbour touches the centre cell
        assert a <= lat_hi + 1e-12 and b >= lat_lo - 1e-12
        assert c2 <= lon_hi + 1e-12 and d >= lon_lo - 1e-12


def test_haversine():
    assert geohash.haversine_m(1.3, 103.8, 1.3, 103.8) == 0
    # one degree of latitude is about 111.2 km
    assert geohash.haversine_m(1.0, 103.8, 2.0, 103.8) == pytest.approx(111_195, rel=1e-3)


def test_min_cell_extent_covers_api_radius():
    # The API caps radius at 500 m because a 3x3 block of precision-6 cells
    # covers at least one full cell side around the query point.
    assert geohash.cell_min_extent_m(6, 1.35) > 500


def test_nine_cell_search_matches_brute_force():
    rnd = random.Random(7)
    pts = [(rnd.uniform(1.27, 1.33), rnd.uniform(103.80, 103.88)) for _ in range(3000)]
    for _ in range(200):
        q = (rnd.uniform(1.28, 1.32), rnd.uniform(103.81, 103.87))
        radius = rnd.choice([50, 200, 500])
        cells = set(geohash.neighbours(geohash.encode(*q, 6)))
        fast = {p for p in pts if geohash.encode(*p, 6) in cells and geohash.haversine_m(*q, *p) <= radius}
        brute = {p for p in pts if geohash.haversine_m(*q, *p) <= radius}
        assert fast == brute
