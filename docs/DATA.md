# Data: sources, zones, dictionary, quality rules

## Source and licence

Contains information from LTA DataMall, made available under the terms of the
Singapore Open Data Licence. DataMall states that use of its datasets and APIs
constitutes acceptance of that licence and of its API Terms of Service.

The fixtures in `fixtures/datamall/` are real DataMall responses, captured
with a DataMall account key by a separate project of mine and replayed here
so the stack needs no key:

| Dataset | Endpoint | Captured | Subset kept | Records |
|---|---|---|---|---|
| Bus stops | `BusStops` | 2026-09-26 | stops between 1.24-1.47 N and 103.75-104.05 E | 4,126 (9 pages) |
| Road speed bands | `v4/TrafficSpeedBands` | 2026-09-29, 7 snapshots 16:41-18:28 SGT, about 18 min apart | links starting between 1.28-1.31 N and 103.83-103.86 E (central area) | 6,267 per snapshot (13 pages each) |

Speed bands were captured as compact CSV (link ID, band, minimum, maximum)
plus one link-geometry table. `scripts/build_fixtures.py` re-expands them
into the DataMall v4 page envelope (`{"odata.metadata": ..., "value": [...]}`,
500 records per page). The values are unchanged; JSON types for the speed
fields are reconstructed as strings, which is what the API returns. Speed
pages are gzipped to keep the repository small.

## Flow and zones

```
EventBridge (rate 15 min / daily, disabled in tests)
   -> ingest Lambda --- reads ---> replay bucket  datamall/...        (stand-in for the API)
        |          --- writes --> lake bucket    raw/<dataset>/snapshot=<id>/page-NNN.json
        |          --- sends ---> SQS pages queue (one message per page, pointer only)
        v
   process Lambda (SQS event source, batch 5, partial batch failures)
        |-- valid records ----> DynamoDB stops / speeds
        |-- rejected records -> lake bucket    rejected/<dataset>/snapshot=<id>/page-NNN.json
        |-- page summary -----> DynamoDB meta
        '-- after 3 failed receives -> DLQ -> CloudWatch alarm -> SNS
   API Gateway (REST) -> api Lambda -> DynamoDB (read only)
```

Raw objects expire after 90 days and rejected objects after 180 days (S3
lifecycle). Speed readings carry a 30-day DynamoDB TTL counted from ingestion.

## Tables

**`<prefix>-stops`** (key `code`)

| Attribute | Type | From | Notes |
|---|---|---|---|
| code | S | BusStopCode | 5 digits |
| description, road | S | Description, RoadName | |
| lat, lon | N | Latitude, Longitude | |
| gh6 | S | derived | geohash, precision 6 (about 1.2 x 0.6 km); GSI `by_geohash` (gh6, code) |
| captured_at | S | capture date | ISO 8601, +08:00 |
| updated_at | S | ingestion time | UTC |

**`<prefix>-speeds`** (key `link_id`, sort `captured_at`)

| Attribute | Type | From | Notes |
|---|---|---|---|
| link_id | S | LinkID | |
| captured_at | S | snapshot time | ISO 8601, +08:00 |
| snapshot | S | | `YYYYMMDD_HHMM` |
| band | N | SpeedBand | 1 = 0-9 km/h ... 8 = 70 km/h and above |
| min_kmh, max_kmh | N | MinimumSpeed, MaximumSpeed | band 8 has max 999 in the source |
| road, category | S | RoadName, RoadCategory | categories 1-6 and 8 occur in v4 |
| expires_at | N | | TTL, ingestion + 30 days |

**`<prefix>-meta`** (key `pk`, sort `sk`)

| pk | sk | Holds |
|---|---|---|
| `<dataset>#<snapshot>` | `page#NNN` | per-page totals, accepted, rejected, reasons, warnings, band counts per road category |
| `snapshots#<dataset>` | `<snapshot>` | index of ingested snapshots |
| `run#<dataset>` | `<started>#<run_id>` | ingestion runs: pages, records |
| `cursor` | `TrafficSpeedBands` | last replayed snapshot (scheduled runs take the next one) |

## Quality rules (`src/transit/quality.py`)

Rejected records go to the `rejected/` zone with their reasons. Warnings are
counted and the record is kept.

| Dataset | Check | Result |
|---|---|---|
| Bus stops | code is 5 digits | reject `bad_stop_code` |
| | latitude and longitude parse, are in range and not (0, 0) | reject `bad_coordinates` |
| | inside the Singapore box 1.15-1.48 N, 103.59-104.10 E | warn `outside_sg_bbox` (DataMall lists some cross-border stops in Johor) |
| | description, road name present | warn |
| Speed bands | link ID numeric | reject `bad_link_id` |
| | band 0 with empty minimum and maximum | reject `no_reading` |
| | band in 1-8 | reject `bad_speed_band` |
| | 0 <= minimum <= maximum | reject `bad_speed_range` |
| | coordinates parse | reject `bad_coordinates` |
| | road category known | warn |
| Both | key repeated within a page | reject `duplicate_key` |

On the fixtures: all 4,126 bus stops pass, and each speed-band snapshot has
exactly 2 `no_reading` links (14 of 43,869 across the seven snapshots).

## Congestion summary

`GET /v1/snapshots/{snapshot}/congestion` adds up the page summaries for one
snapshot. For each road category it returns the number of links, the mean
band, and the share of links in bands 1-2 (below 20 km/h). It reads at most 13
items, not 6,267, because the process Lambda writes the per-page counts.
