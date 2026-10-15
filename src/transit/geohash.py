"""Geohash encoding, neighbour cells and haversine distance (stdlib only)."""
import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
EARTH_RADIUS_M = 6_371_008.8


def encode(lat: float, lon: float, precision: int = 6) -> str:
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(out)


def bounds(gh: str):
    """Return (lat_lo, lat_hi, lon_lo, lon_hi) of a geohash cell."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    for c in gh:
        v = _BASE32.index(c)
        for shift in range(4, -1, -1):
            bit = (v >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                if bit:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def neighbours(gh: str):
    """The cell itself plus its 8 neighbours (computed from cell centres)."""
    lat_lo, lat_hi, lon_lo, lon_hi = bounds(gh)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    clat, clon = (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2
    cells = []
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            cells.append(encode(clat + i * dlat, clon + j * dlon, len(gh)))
    return sorted(set(cells))


def cell_min_extent_m(precision: int, lat: float) -> float:
    """Smaller side of a geohash cell in metres at the given latitude."""
    lat_lo, lat_hi, lon_lo, lon_hi = bounds(encode(lat, 0.0, precision))
    h = (lat_hi - lat_lo) * math.pi / 180 * EARTH_RADIUS_M
    w = (lon_hi - lon_lo) * math.pi / 180 * EARTH_RADIUS_M * math.cos(math.radians(lat))
    return min(h, w)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
