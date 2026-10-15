from collections import Counter

from conftest import FIXTURES, load_page
from transit.quality import validate_speed, validate_stop

GOOD_STOP = {"BusStopCode": "01012", "RoadName": "Victoria St", "Description": "Hotel Grand Pacific",
             "Latitude": 1.29684825487647, "Longitude": 103.85253591654006}
GOOD_SPEED = {"LinkID": "103000000", "RoadName": "KENT ROAD", "RoadCategory": "5", "SpeedBand": 3,
              "MinimumSpeed": "20", "MaximumSpeed": "29", "StartLon": "103.84", "StartLat": "1.29",
              "EndLon": "103.841", "EndLat": "1.291"}


def test_good_stop():
    rec, reasons, warnings = validate_stop(GOOD_STOP)
    assert reasons == [] and warnings == []
    assert rec["code"] == "01012" and float(rec["lat"]) == GOOD_STOP["Latitude"]


def test_stop_rejections():
    cases = {
        "bad_stop_code": [{"BusStopCode": "1012"}, {"BusStopCode": "0101A"}, {"BusStopCode": ""}],
        "bad_coordinates": [{"Latitude": "abc"}, {"Latitude": 0, "Longitude": 0}, {"Latitude": 95},
                            {"Longitude": None}, {"Latitude": "nan"}],
    }
    for reason, overrides in cases.items():
        for o in overrides:
            rec, reasons, _ = validate_stop({**GOOD_STOP, **o})
            assert rec is None and reason in reasons, o


def test_cross_border_stop_is_kept_with_warning():
    # DataMall lists some stops across the Causeway in Johor.
    rec, reasons, warnings = validate_stop({**GOOD_STOP, "Latitude": 1.4939, "Longitude": 103.744})
    assert rec is not None and reasons == [] and warnings == ["outside_sg_bbox"]


def test_empty_text_fields_warn():
    rec, reasons, warnings = validate_stop({**GOOD_STOP, "Description": " ", "RoadName": ""})
    assert rec is not None and set(warnings) == {"empty_description", "empty_road_name"}


def test_good_speed():
    rec, reasons, warnings = validate_speed(GOOD_SPEED)
    assert reasons == [] and warnings == []
    assert (rec["band"], rec["min_kmh"], rec["max_kmh"]) == (3, 20, 29)


def test_speed_rejections():
    cases = [
        ({"SpeedBand": 0, "MinimumSpeed": "", "MaximumSpeed": ""}, "no_reading"),
        ({"SpeedBand": 9}, "bad_speed_band"),
        ({"SpeedBand": 0}, "bad_speed_band"),
        ({"SpeedBand": "x"}, "bad_speed_band"),
        ({"MinimumSpeed": "40", "MaximumSpeed": "29"}, "bad_speed_range"),
        ({"MinimumSpeed": "-1"}, "bad_speed_range"),
        ({"MaximumSpeed": ""}, "bad_speed_range"),
        ({"LinkID": "12a"}, "bad_link_id"),
        ({"StartLat": "?"}, "bad_coordinates"),
    ]
    for o, reason in cases:
        rec, reasons, _ = validate_speed({**GOOD_SPEED, **o})
        assert rec is None and reason in reasons, (o, reasons)


def test_band_eight_upper_bound_is_open():
    rec, reasons, _ = validate_speed({**GOOD_SPEED, "SpeedBand": 8, "MinimumSpeed": "70", "MaximumSpeed": "999"})
    assert rec is not None and rec["max_kmh"] == 999


def test_unknown_category_warns():
    rec, _, warnings = validate_speed({**GOOD_SPEED, "RoadCategory": "Z"})
    assert rec is not None and warnings == ["unknown_road_category"]


def test_real_fixture_quality(manifest):
    """The committed captures: every stop passes; the 17:53 snapshot has exactly
    two links with no reading (band 0, empty speeds)."""
    stops = Counter()
    for f in sorted((FIXTURES / "BusStops").glob("page-*.json")):
        for r in load_page(f)["value"]:
            rec, reasons, _ = validate_stop(r)
            stops["ok" if rec else "bad"] += 1
    assert stops == {"ok": manifest["bus_stops"]}

    reasons = Counter()
    n = 0
    for f in sorted((FIXTURES / "TrafficSpeedBands" / "20260929_1753").glob("page-*.json.gz")):
        for r in load_page(f)["value"]:
            n += 1
            reasons.update(validate_speed(r)[1])
    assert n == manifest["snapshots"]["20260929_1753"]["records"]
    assert reasons == {"no_reading": 2}
