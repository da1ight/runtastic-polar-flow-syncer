"""Upload a Runtastic (adidas Running) export to Polar Flow.

Uses the same unofficial Polar Beat API (www.polarremote.com) that the
SyncMyTracks app uses. Message layouts were taken from the protobuf-lite
classes in SyncMyTracks 3.13.2 (com.syncmytracks.proto.*).

Usage:
  python3 -I polar_upload.py <export_dir> --dry-run [--only PREFIX] [--out DIR]
  python3 -I polar_upload.py <export_dir> --email you@example.com [--only PREFIX] [--limit N]

  --only PREFIX   upload only sessions whose file name starts with PREFIX
                  (e.g. 2016-09-09 or 2016-09-09_17-37)
  --limit N       stop after N successful uploads
  --dry-run       build the protobuf messages and write them to --out, no network
  -v, --verbose   print every API request and its answer

"uploaded*" means all data was sent but the finalize call was rejected (SyncMyTracks
ignores that answer too) - check the session in Polar Flow.

Uploaded sessions are recorded in <export_dir>/polar-uploaded.json and skipped
on re-runs; the server also answers 200 for a session it already has.
"""
import argparse
import bisect
import getpass
import glob
import http.cookiejar
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.polarremote.com/v2"
# Polar Beat Android app credentials, as sent by SyncMyTracks ("Polar Beat2 Android:Beat2AndroidApiKey")
APP_AUTH = "Basic UG9sYXIgQmVhdDIgQW5kcm9pZDpCZWF0MkFuZHJvaWRBcGlLZXk="
USER_AGENT = "PolarBeat-Android/3.4.4 Dalvik/2.1.0 (Linux; U; Android 6.0; ALE-L21 Build/HuaweiALE-L21)"

# Runtastic sport_type_id -> Polar sport id
SPORTS = {"1": 1, "3": 2}  # running, cycling
GAP_PAUSE_MS = 60_000  # longer gaps between GPS points are treated as a pause (speed 0)
HR_MAX_GAP_MS = 15_000


# ---------------------------------------------------------------- protobuf encoding

def _varint(n):
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field, wire):
    return _varint(field << 3 | wire)


def _zigzag(n):
    return (n << 1) ^ (n >> 31)


class Msg:
    """Minimal proto2 message writer; fields are emitted in call order."""

    def __init__(self):
        self.buf = bytearray()

    def uint(self, field, value):  # uint32 / uint64 / int32 / enum
        self.buf += _key(field, 0) + _varint(int(value))
        return self

    def bool(self, field, value):
        return self.uint(field, 1 if value else 0)

    def float(self, field, value):
        self.buf += _key(field, 5) + struct.pack("<f", value)
        return self

    def double(self, field, value):
        self.buf += _key(field, 1) + struct.pack("<d", value)
        return self

    def bytes(self, field, data):
        self.buf += _key(field, 2) + _varint(len(data)) + data
        return self

    def string(self, field, text):
        return self.bytes(field, text.encode("utf-8"))

    def msg(self, field, sub):
        return self.bytes(field, bytes(sub.buf))

    def packed_uint(self, field, values):
        return self.bytes(field, b"".join(_varint(int(v)) for v in values)) if values else self

    def packed_sint(self, field, values):
        return self.bytes(field, b"".join(_varint(_zigzag(int(v))) for v in values)) if values else self

    def packed_float(self, field, values):
        return self.bytes(field, struct.pack(f"<{len(values)}f", *values)) if values else self

    def packed_double(self, field, values):
        return self.bytes(field, struct.pack(f"<{len(values)}d", *values)) if values else self

    def serialize(self):
        return bytes(self.buf)


def pb_duration(seconds):
    ms = int(round(seconds * 1000))
    return (Msg().uint(1, ms // 3_600_000).uint(2, ms // 60_000 % 60)
            .uint(3, ms // 1000 % 60).uint(4, ms % 1000))


def pb_local_datetime(utc_ms, offset_minutes):
    t = time.gmtime((utc_ms + offset_minutes * 60_000) // 1000)
    date = Msg().uint(1, t.tm_year).uint(2, t.tm_mon).uint(3, t.tm_mday)
    tod = Msg().uint(1, t.tm_hour).uint(2, t.tm_min).uint(3, t.tm_sec).uint(4, utc_ms % 1000)
    return Msg().msg(1, date).msg(2, tod).bool(3, True).uint(4, offset_minutes)


# ---------------------------------------------------------------- Runtastic data

def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def feature(session, name):
    return next((f["attributes"] for f in session["features"] if f["type"] == name), {})


def interp(xs, ys, x):
    """Linear interpolation of ys over sorted xs, clamped at the ends."""
    i = bisect.bisect_right(xs, x)
    if i == 0:
        return ys[0]
    if i == len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


def build_series(gps, elev, hr):
    """Resample a Runtastic track to 1-second series, as Polar Beat records them."""
    pts, seen = [], set()
    for p in sorted(gps, key=lambda p: p["timestamp"]):
        if p["timestamp"] not in seen:
            seen.add(p["timestamp"])
            pts.append(p)
    corrected = {e["timestamp"]: e["elevation"] for e in elev or [] if e.get("source_type") == 11}

    ts = [p["timestamp"] for p in pts]
    lat = [p["latitude"] for p in pts]
    lon = [p["longitude"] for p in pts]
    alt = [float(corrected.get(p["timestamp"], p.get("altitude") or 0)) for p in pts]
    dist, d = [], 0.0
    for p in pts:
        d = max(d, float(p.get("distance") or 0))  # keep cumulative distance non-decreasing
        dist.append(d)

    hr = sorted((h for h in hr or [] if h.get("heart_rate")), key=lambda h: h["timestamp"])
    hr_ts = [h["timestamp"] for h in hr]
    hr_v = [float(h["heart_rate"]) for h in hr]

    start, end = ts[0], ts[-1]
    s = {"start": start, "end": end, "offset_ms": [], "lat": [], "lon": [], "alt": [],
         "dist": [], "speed": [], "hr": []}
    prev_dist = None
    for t in range(start, end + 1, 1000):
        i = bisect.bisect_right(ts, t)
        in_pause = 0 < i < len(ts) and ts[i] - ts[i - 1] > GAP_PAUSE_MS
        dd = interp(ts, dist, t)
        s["offset_ms"].append(t - start)
        s["lat"].append(interp(ts, lat, t))
        s["lon"].append(interp(ts, lon, t))
        s["alt"].append(interp(ts, alt, t))
        s["dist"].append(dd)
        s["speed"].append(0.0 if in_pause or prev_dist is None else min(max(dd - prev_dist, 0.0) * 3.6, 200.0))
        prev_dist = dd
        if hr_ts:
            j = bisect.bisect_left(hr_ts, t)
            near = min((abs(hr_ts[k] - t) for k in (j - 1, j) if 0 <= k < len(hr_ts)), default=None)
            s["hr"].append(round(interp(hr_ts, hr_v, t)) if near is not None and near <= HR_MAX_GAP_MS else 0)
    if s["hr"] and not any(s["hr"]):
        s["hr"] = []
    return s


def build_messages(session, gps, elev, hr):
    sport_id = SPORTS.get(session.get("sport_type_id"))
    if sport_id is None:
        raise ValueError(f"unknown Runtastic sport_type_id {session.get('sport_type_id')}")
    tm = feature(session, "track_metrics")
    hra = feature(session, "heart_rate")
    offset_min = int(session.get("start_time_timezone_offset") or 0) // 60_000
    distance = float(tm.get("distance") or 0)
    calories = int(session.get("calories") or 0)

    series = build_series(gps, elev, hr) if gps else None
    if series:
        start_ms = series["start"]
        duration_s = (series["end"] - series["start"]) / 1000
    else:
        start_ms = session["start_time"]
        duration_s = session["duration"] / 1000
    start = pb_local_datetime(start_ms, offset_min)
    duration = pb_duration(duration_s)

    note = "Imported from Runtastic"
    w = feature(session, "weather")
    if w.get("conditions"):
        note += f"; weather: {w['conditions']}"
        if w.get("temperature") is not None:
            note += f", {w['temperature']:g}°C"

    training_session = (Msg().msg(1, start).uint(2, 1).string(4, "Polar Beat").msg(5, duration)
                        .float(6, distance).uint(7, calories).msg(13, Msg().string(1, note)))

    exercise = (Msg().msg(1, start).msg(2, duration).msg(3, Msg().uint(1, sport_id))
                .float(4, distance).uint(5, calories))
    if series:
        elev_last = (elev or [{}])[-1]
        ascent = elev_last.get("elevation_gain", tm.get("elevation_gain") or 0)
        descent = elev_last.get("elevation_loss", tm.get("elevation_loss") or 0)
        exercise.float(10, float(ascent)).float(11, float(descent))
        exercise.double(12, series["lat"][0]).double(13, series["lon"][0])

    stats = Msg()
    hr_vals = [v for v in series["hr"] if v] if series else []
    if hr_vals:
        stats.msg(1, Msg().uint(1, min(hr_vals)).uint(2, round(sum(hr_vals) / len(hr_vals)))
                  .uint(3, min(max(hr_vals), 210)))
    elif hra.get("average"):
        stats.msg(1, Msg().uint(2, int(hra["average"])).uint(3, min(int(hra.get("maximum") or 0), 210)))
    if duration_s > 0 and distance > 0:
        avg_kmh = distance / duration_s * 3.6
        max_kmh = float(tm.get("max_speed") or 0) * 3.6 or avg_kmh
        stats.msg(2, Msg().float(1, avg_kmh).float(2, min(max(max_kmh, avg_kmh), 350.0)))
    if series:
        a = series["alt"]
        stats.msg(4, Msg().float(1, min(a)).float(2, sum(a) / len(a)).float(3, max(a)))

    samples = route = None
    if series and len(series["offset_ms"]) > 2:
        samples = (Msg().msg(1, pb_duration(1))
                   .packed_uint(2, series["hr"])
                   .packed_float(6, series["alt"])
                   .packed_float(9, series["speed"])
                   .packed_float(11, series["dist"]))
        route = (Msg().packed_uint(1, series["offset_ms"])
                 .packed_double(2, series["lat"])
                 .packed_double(3, series["lon"])
                 .packed_sint(4, [round(v) for v in series["alt"]]))

    info = {"sport": {1: "running", 2: "cycling"}[sport_id], "km": round(distance / 1000, 2),
            "duration_min": round(duration_s / 60, 1), "samples": len(series["offset_ms"]) if series else 0,
            "hr_samples": len(hr_vals)}
    return {"training_session": training_session.serialize(), "exercise_base": exercise.serialize(),
            "statistics": stats.serialize() if stats.buf else None,
            "samples": samples.serialize() if samples else None,
            "route": route.serialize() if route else None}, info


# ---------------------------------------------------------------- Polar API

class PolarClient:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _post(self, url, body, content_type, accept=None):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", APP_AUTH)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", content_type)
        if accept:
            req.add_header("Accept", accept)
        try:
            with self.opener.open(req, timeout=60) as r:
                code, loc, data = r.status, r.headers.get("Location"), r.read()
        except urllib.error.HTTPError as e:
            code, loc, data = e.code, e.headers.get("Location"), e.read()
        return code, urllib.parse.urljoin(url, loc) if loc else None, data

    def login(self, email, password):
        body = json.dumps({"password": password, "username": email}).encode()
        code, _, data = self._post(f"{API}/login/user/associateToApp", body,
                                   "Application/json", "Application/json")
        if code != 200:
            raise RuntimeError(f"login failed: HTTP {code}: {data[:300]!r}")
        self.user_id = json.loads(data)["id"]
        return self.user_id

    def post_pb(self, url, payload):
        code, loc, data = self._post(url, payload, "application/x-protobuf", "application/x-protobuf")
        self.log(url, code, loc, data)
        return code, loc, data

    def log(self, url, code, loc, data):
        if self.verbose:
            print(f"    POST {url} -> {code}" + (f", Location: {loc}" if loc else "")
                  + (f", body: {data[:200]!r}" if data and code >= 300 else ""))

    def finalize(self, url):
        url = f"{url}/finalize?success=true"
        code, loc, data = self._post(url, b"", "application/x-www-form-urlencoded; charset=UTF-8")
        self.log(url, code, loc, data)
        return code < 300

    def upload(self, m):
        code, loc, data = self.post_pb(f"{API}/users/{self.user_id}/training-sessions/create",
                                       m["training_session"])
        if code == 200:
            return "duplicate", None
        if code != 201 or not loc:
            raise RuntimeError(f"training-sessions/create: HTTP {code}, Location={loc}, body={data[:300]!r}")
        session_url = loc
        code, ex_url, data = self.post_pb(f"{session_url}/exercises/create", m["exercise_base"])
        if code not in (200, 201) or not ex_url:
            raise RuntimeError(f"exercises/create: HTTP {code}, Location={ex_url}, body={data[:300]!r}")
        for part in ("statistics", "samples", "route"):
            if m[part]:
                code, _, data = self.post_pb(f"{ex_url}/{part}", m[part])
                if code >= 300:
                    raise RuntimeError(f"{part}: HTTP {code}, body={data[:300]!r}")
        # SyncMyTracks sends finalize to the exercise URL but never checks the answer,
        # and the server replies 404 there; try the session URL too and don't fail on it.
        if self.finalize(ex_url) or self.finalize(session_url):
            return "uploaded", session_url
        return "uploaded*", session_url


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir")
    ap.add_argument("--email")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every API request and its answer")
    ap.add_argument("--out", default="polar-dry-run")
    args = ap.parse_args()

    ss = os.path.join(args.export_dir, "Sport-sessions")
    state_path = os.path.join(args.export_dir, "polar-uploaded.json")
    state = load(state_path) or {}
    paths = [p for p in sorted(glob.glob(os.path.join(ss, "*.json")))
             if os.path.basename(p).startswith(args.only)]
    if not paths:
        sys.exit(f"no sessions matching '{args.only}' in {ss}")

    client = None
    if not args.dry_run:
        email = args.email or input("Polar email: ")
        client = PolarClient(args.verbose)
        print(f"logged in, Polar user id {client.login(email, getpass.getpass('Polar password: '))}")

    done = 0
    failed = []
    for path in paths:
        base = os.path.basename(path)[:-5]
        if not args.dry_run and base in state:
            print(f"skip      {base} (already uploaded)")
            continue
        session = load(path)
        msgs, info = build_messages(session,
                                    load(os.path.join(ss, "GPS-data", base + ".json")),
                                    load(os.path.join(ss, "Elevation-data", base + ".json")),
                                    load(os.path.join(ss, "Heart-rate-data", base + ".json")))
        desc = (f"{info['sport']:<8} {info['km']:6.2f} km {info['duration_min']:6.1f} min "
                f"{info['samples']:5d} samples, {info['hr_samples']} hr")
        if args.dry_run:
            d = os.path.join(args.out, base)
            os.makedirs(d, exist_ok=True)
            for name, data in msgs.items():
                if data:
                    with open(os.path.join(d, name + ".pb"), "wb") as f:
                        f.write(data)
            print(f"built     {base}  {desc}")
            continue
        try:
            result, url = client.upload(msgs)
        except Exception as e:
            print(f"FAILED    {base}: {e}")
            failed.append(base)
            if len(failed) >= 3 and not done:
                sys.exit("three failures and no successful upload, stopping")
            continue
        print(f"{result:<9} {base}  {desc}")
        state[base] = {"result": result, "url": url, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        done += 1
        if args.limit and done >= args.limit:
            break
        time.sleep(1)

    if failed:
        print(f"\n{len(failed)} failed (re-run to retry): " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
