"""Record validation for the two DataMall datasets.

Each validator returns (clean_record | None, reasons, warnings). A record with
any reason is rejected and written to the rejected/ zone; warnings are kept
with the record and counted.
"""
import re
from decimal import Decimal, InvalidOperation

# Singapore main island plus offshore islands. Some DataMall bus stops sit
# across the Causeway in Johor (cross-border services), so a stop outside this
# box is a warning, not a rejection.
SG_BBOX = (1.15, 1.48, 103.59, 104.10)
STOP_CODE = re.compile(r"^\d{5}$")
LINK_ID = re.compile(r"^\d{1,12}$")
VALID_BANDS = range(1, 9)  # DataMall v4: 1 = 0-9 km/h ... 8 = 70+ km/h
ROAD_CATEGORIES = {"1", "2", "3", "4", "5", "6", "8"}  # observed in v4 (7 unused)


def _dec(v):
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _in_sg(lat, lon):
    return SG_BBOX[0] <= lat <= SG_BBOX[1] and SG_BBOX[2] <= lon <= SG_BBOX[3]


def validate_stop(r):
    reasons, warnings = [], []
    code = str(r.get("BusStopCode", "")).strip()
    if not STOP_CODE.match(code):
        reasons.append("bad_stop_code")
    lat, lon = _dec(r.get("Latitude")), _dec(r.get("Longitude"))
    if lat is None or lon is None:
        reasons.append("bad_coordinates")
    elif not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        reasons.append("bad_coordinates")
    elif not _in_sg(lat, lon):
        warnings.append("outside_sg_bbox")
    desc = str(r.get("Description", "")).strip()
    road = str(r.get("RoadName", "")).strip()
    if not desc:
        warnings.append("empty_description")
    if not road:
        warnings.append("empty_road_name")
    if reasons:
        return None, reasons, warnings
    return {"code": code, "description": desc, "road": road,
            "lat": lat, "lon": lon}, reasons, warnings


def validate_speed(r):
    reasons, warnings = [], []
    link = str(r.get("LinkID", "")).strip()
    if not LINK_ID.match(link):
        reasons.append("bad_link_id")
    try:
        band = int(r.get("SpeedBand"))
    except (TypeError, ValueError):
        band = None
    lo_raw, hi_raw = str(r.get("MinimumSpeed", "")).strip(), str(r.get("MaximumSpeed", "")).strip()
    if band == 0 and not lo_raw and not hi_raw:
        reasons.append("no_reading")  # DataMall sends band 0 with empty speeds when a link has no data
    elif band not in VALID_BANDS:
        reasons.append("bad_speed_band")
    lo, hi = _dec(lo_raw), _dec(hi_raw)
    if "no_reading" not in reasons:
        if lo is None or hi is None:
            reasons.append("bad_speed_range")
        elif lo < 0 or lo > hi:
            reasons.append("bad_speed_range")
    cat = str(r.get("RoadCategory", "")).strip()
    if cat not in ROAD_CATEGORIES:
        warnings.append("unknown_road_category")
    coords = [_dec(r.get(k)) for k in ("StartLat", "StartLon", "EndLat", "EndLon")]
    if any(c is None for c in coords):
        reasons.append("bad_coordinates")
    elif not (_in_sg(coords[0], coords[1]) and _in_sg(coords[2], coords[3])):
        warnings.append("outside_sg_bbox")
    if reasons:
        return None, reasons, warnings
    return {"link_id": link, "band": band, "min_kmh": int(lo), "max_kmh": int(hi),
            "road": str(r.get("RoadName", "")).strip(), "category": cat,
            "start": (coords[0], coords[1]), "end": (coords[2], coords[3])}, reasons, warnings
