"""Build the replay fixtures from captured LTA DataMall payloads.

The captures were taken with a DataMall account key by a separate project
(bus stops on 2026-09-26, speed bands every ~18 minutes on 2026-09-29).
Speed bands were stored as compact CSV (LinkID, band, min, max) plus one
link-geometry table, so this script re-expands them into the DataMall v4 JSON
page envelope. JSON types for the speed fields are reconstructed (strings, as
the API returns them); the values are unchanged.

Usage: python scripts/build_fixtures.py <capture_dir>
"""
import csv
import gzip
import json
import pathlib
import sys

BBOX = (1.28, 1.31, 103.83, 103.86)  # speed-band links: lat_min, lat_max, lon_min, lon_max (central area)
STOP_BBOX = (1.24, 1.47, 103.75, 104.05)  # bus stops: mainland east of 103.75 E
SNAPSHOTS = ["20260929_1641", "20260929_1659", "20260929_1717", "20260929_1735",
             "20260929_1753", "20260929_1810", "20260929_1828"]
PAGE = 500
BASE = "https://datamall2.mytransport.sg/ltaodataservice"
OUT = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "datamall"


def inside(lat, lon, bb=BBOX):
    return bb[0] <= lat <= bb[1] and bb[2] <= lon <= bb[3]


def write_pages(dirpath, endpoint, rows, gz):
    dirpath.mkdir(parents=True, exist_ok=True)
    for f in dirpath.glob("page-*"):
        f.unlink()
    for i in range(0, max(len(rows), 1), PAGE):
        body = {"odata.metadata": f"{BASE}/{endpoint}/$metadata", "value": rows[i:i + PAGE]}
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        name = dirpath / f"page-{i // PAGE:03d}.json"
        if gz:
            # mtime=0 keeps the gzip bytes deterministic across rebuilds
            with gzip.GzipFile(str(name) + ".gz", "wb", mtime=0) as z:
                z.write(data)
        else:
            name.write_bytes(data)


def main(src):
    src = pathlib.Path(src)
    stops = json.loads((src / "BusStops_20260926.json").read_text(encoding="utf-8"))
    stops = [s for s in stops if inside(s["Latitude"], s["Longitude"], STOP_BBOX)]
    stops.sort(key=lambda s: s["BusStopCode"])
    write_pages(OUT / "BusStops", "BusStops", stops, gz=False)

    links = {}
    with open(src / "speedband_links.csv", newline="") as f:
        for r in csv.DictReader(f):
            if inside(float(r["StartLat"]), float(r["StartLon"])):
                links[r["LinkID"]] = r
    manifest = {"bbox_links": BBOX, "bbox_stops": STOP_BBOX, "bus_stops": len(stops), "bus_stops_captured": "20260926", "snapshots": {}}
    for snap in SNAPSHOTS:
        rows = []
        with gzip.open(src / "speedbands" / f"sb_{snap}.csv.gz", "rt", newline="") as f:
            for r in csv.DictReader(f):
                m = links.get(r["LinkID"])
                if m is None:
                    continue
                rows.append({
                    "LinkID": r["LinkID"], "RoadName": m["RoadName"], "RoadCategory": m["RoadCategory"],
                    "SpeedBand": int(r["SpeedBand"]), "MinimumSpeed": r["MinimumSpeed"],
                    "MaximumSpeed": r["MaximumSpeed"], "StartLon": m["StartLon"], "StartLat": m["StartLat"],
                    "EndLon": m["EndLon"], "EndLat": m["EndLat"],
                })
        rows.sort(key=lambda r: int(r["LinkID"]))
        ts = f"{snap[:4]}-{snap[4:6]}-{snap[6:8]}T{snap[9:11]}:{snap[11:13]}:00+08:00"
        write_pages(OUT / "TrafficSpeedBands" / snap, "v4/TrafficSpeedBands", rows, gz=True)
        manifest["snapshots"][snap] = {"captured_at": ts, "records": len(rows)}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
