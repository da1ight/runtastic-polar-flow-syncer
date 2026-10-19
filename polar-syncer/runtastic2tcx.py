"""Convert a Runtastic (adidas Running) export to Garmin TCX files.

Usage: python3 -I runtastic2tcx.py <export_dir> <out_dir>

Per session it merges:
  Sport-sessions/<id>.json                   summary (sport, duration, distance, calories, HR avg/max)
  Sport-sessions/GPS-data/<id>.json          track points (lat/lon/alt/cumulative distance)
  Sport-sessions/Elevation-data/<id>.json    corrected elevation (preferred over raw GPS altitude)
  Sport-sessions/Heart-rate-data/<id>.json   heart-rate samples
"""
import bisect
import datetime as dt
import glob
import json
import os
import sys
from xml.sax.saxutils import escape

SPORTS = {"1": "Running", "3": "Biking"}  # anything else -> Other
HR_MAX_GAP_MS = 15_000  # attach an HR sample to a track point only if this close in time


def iso(ms):
    t = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def feature(session, name):
    return next((f["attributes"] for f in session["features"] if f["type"] == name), {})


def nearest_hr(hr_ts, hr_vals, ts):
    i = bisect.bisect_left(hr_ts, ts)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(hr_ts):
            gap = abs(hr_ts[j] - ts)
            if gap <= HR_MAX_GAP_MS and (best is None or gap < best[0]):
                best = (gap, hr_vals[j])
    return best[1] if best else None


def trackpoints(gps, elev, hr):
    gps = sorted(gps, key=lambda p: p["timestamp"])
    elev_by_ts = {e["timestamp"]: e["elevation"] for e in elev or [] if e.get("source_type") == 11}
    hr = sorted((h for h in hr or [] if h.get("heart_rate")), key=lambda h: h["timestamp"])
    hr_ts = [h["timestamp"] for h in hr]
    hr_vals = [h["heart_rate"] for h in hr]

    stats = {"points": 0, "elev_corrected": 0, "hr": 0}
    out = []
    dist = 0.0
    last_ts = None
    for p in gps:
        if p["timestamp"] == last_ts:
            continue  # TCX importers dislike duplicate timestamps
        last_ts = p["timestamp"]
        dist = max(dist, float(p.get("distance") or 0))  # keep cumulative distance non-decreasing
        alt = elev_by_ts.get(p["timestamp"])
        if alt is not None:
            stats["elev_corrected"] += 1
        else:
            alt = p.get("altitude")
        bpm = nearest_hr(hr_ts, hr_vals, p["timestamp"]) if hr_ts else None
        if bpm:
            stats["hr"] += 1
        x = [f"<Trackpoint><Time>{iso(p['timestamp'])}</Time>",
             f"<Position><LatitudeDegrees>{p['latitude']:.7f}</LatitudeDegrees>"
             f"<LongitudeDegrees>{p['longitude']:.7f}</LongitudeDegrees></Position>"]
        if alt is not None:
            x.append(f"<AltitudeMeters>{float(alt):.1f}</AltitudeMeters>")
        x.append(f"<DistanceMeters>{dist:.1f}</DistanceMeters>")
        if bpm:
            x.append(f"<HeartRateBpm><Value>{int(bpm)}</Value></HeartRateBpm>")
        x.append("</Trackpoint>")
        out.append("".join(x))
        stats["points"] += 1
    return out, stats


def notes(session):
    parts = ["Imported from Runtastic"]
    w = feature(session, "weather")
    if w:
        bits = [w.get("conditions")]
        if w.get("temperature") is not None:
            bits.append(f"{w['temperature']:g}°C")
        parts.append("weather: " + ", ".join(b for b in bits if b))
    dev = feature(session, "origin").get("device", {})
    if dev.get("name"):
        parts.append(f"device: {dev.get('vendor', '')} {dev['name']}".strip())
    if session.get("tracking_method") == "manual":
        parts.append("manual entry")
    return "; ".join(parts)


def convert(session, gps, elev, hr):
    sport = SPORTS.get(session.get("sport_type_id"), "Other")
    tm = feature(session, "track_metrics")
    hra = feature(session, "heart_rate")
    start = session["start_time"]
    distance = float(tm.get("distance") or 0)

    pts, stats = trackpoints(gps, elev, hr) if gps else ([], {"points": 0, "elev_corrected": 0, "hr": 0})

    lap = [f'<Lap StartTime="{iso(start)}">',
           f"<TotalTimeSeconds>{session['duration'] / 1000:.3f}</TotalTimeSeconds>",
           f"<DistanceMeters>{distance:.1f}</DistanceMeters>"]
    if tm.get("max_speed"):
        lap.append(f"<MaximumSpeed>{float(tm['max_speed']):.3f}</MaximumSpeed>")
    lap.append(f"<Calories>{int(session.get('calories') or 0)}</Calories>")
    if hra.get("average"):
        lap.append(f"<AverageHeartRateBpm><Value>{int(hra['average'])}</Value></AverageHeartRateBpm>")
    if hra.get("maximum"):
        lap.append(f"<MaximumHeartRateBpm><Value>{int(hra['maximum'])}</Value></MaximumHeartRateBpm>")
    lap.append("<Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod>")
    if pts:
        lap.append("<Track>\n" + "\n".join(pts) + "\n</Track>")
    lap.append("</Lap>")

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" '
           'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           'xsi:schemaLocation="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2 '
           'http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd">\n'
           f'<Activities>\n<Activity Sport="{sport}">\n<Id>{iso(start)}</Id>\n'
           + "\n".join(lap) +
           f"\n<Notes>{escape(notes(session))}</Notes>\n"
           "</Activity>\n</Activities>\n</TrainingCenterDatabase>\n")
    return xml, sport, distance, stats


def main(export_dir, out_dir):
    ss = os.path.join(export_dir, "Sport-sessions")
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for path in sorted(glob.glob(os.path.join(ss, "*.json"))):
        base = os.path.basename(path)
        session = load(path)
        gps = load(os.path.join(ss, "GPS-data", base))
        elev = load(os.path.join(ss, "Elevation-data", base))
        hr = load(os.path.join(ss, "Heart-rate-data", base))
        xml, sport, distance, st = convert(session, gps, elev, hr)
        name = base[:-5] + ".tcx"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(xml)
        rows.append((name, sport, distance, st))

    print(f"{'file':<62} {'sport':<8} {'km':>6} {'pts':>5} {'elev':>5} {'hr':>4}")
    for name, sport, distance, st in rows:
        print(f"{name:<62} {sport:<8} {distance / 1000:6.2f} {st['points']:5d} {st['elev_corrected']:5d} {st['hr']:4d}")
    print(f"\n{len(rows)} files written to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
