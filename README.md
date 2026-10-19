# runtastic-polar-flow-syncer

Move your workout history from a Runtastic (adidas Running) data export to
[Polar Flow](https://flow.polar.com).

Two standalone Python scripts. Both use only the standard library, so there is nothing to install:

| Script | What it does |
| --- | --- |
| [`polar-syncer/polar_upload.py`](polar-syncer/polar_upload.py) | Uploads sessions directly to Polar Flow through the unofficial Polar Beat API (`www.polarremote.com`). |
| [`polar-syncer/runtastic2tcx.py`](polar-syncer/runtastic2tcx.py) | Converts sessions to Garmin TCX files. You can import these manually into Polar Flow, Strava, Garmin Connect and other services. |

## Getting the Runtastic export

Request a personal data export from adidas Running (Runtastic) and unzip it. The
scripts need the `Sport-sessions/` folder in the export root:

```
<export_dir>/
  Sport-sessions/
    <id>.json                  session summary: sport, duration, distance, calories, HR avg/max, weather
    GPS-data/<id>.json         track points: lat/lon/altitude/cumulative distance
    Elevation-data/<id>.json   corrected elevation (used in place of raw GPS altitude)
    Heart-rate-data/<id>.json  heart-rate samples
```

Session ids look like `2016-09-09_17-37-19-UTC_57d2f34f7f280e9ac25d2bb6`.

## Upload to Polar Flow

Requires Python 3. The `-I` flag runs Python in isolated mode, so it won't import
modules from the export directory.

```sh
# 1. Build the protobuf messages offline and check the summary for each session
python3 -I polar-syncer/polar_upload.py <export_dir> --dry-run

# 2. Upload a single session and check it in Polar Flow
python3 -I polar-syncer/polar_upload.py <export_dir> --email you@example.com --limit 1

# 3. Upload everything else
python3 -I polar-syncer/polar_upload.py <export_dir> --email you@example.com
```

The script prompts for your Polar password. It does not store the password.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Build the messages and write them as `.pb` files to `--out` (default `polar-dry-run/`). No network access. |
| `--email EMAIL` | Polar account email. If omitted, the script asks for it. |
| `--only PREFIX` | Only process sessions whose file name starts with `PREFIX`, e.g. `2016-09-09` or `2016-09-09_17-37`. |
| `--limit N` | Stop after `N` successful uploads. |
| `-v`, `--verbose` | Print every API request and its response. |

### Output

Each session gets one status line:

- `uploaded`: all data was sent and the session was finalized.
- `uploaded*`: all data was sent but the server rejected the finalize call. SyncMyTracks
  ignores this response too. Check that the session appears in Polar Flow.
- `duplicate`: Polar already has a session with this start time.
- `skip`: already recorded in the state file (see below).
- `FAILED`: an error occurred. Re-run the script to retry. If the first three sessions
  fail and none succeed, the run stops.

### Re-runs

The script records uploaded sessions in `<export_dir>/polar-uploaded.json` and skips
them on later runs, so you can stop and resume an upload at any time. Delete an entry
from that file to force a session to upload again.

### How the upload works

For each session, the script:

1. Resamples the GPS track to a 1-second series, the format Polar Beat records in.
   It interpolates position, altitude, distance and heart rate, and derives speed from
   distance. A GPS gap longer than 60 s counts as a pause (speed 0). A heart-rate sample
   is only used if it is within 15 s of the point.
2. Encodes the session as proto2 messages using a small hand-written encoder, so no
   `protobuf` dependency is needed. The message layouts come from the SyncMyTracks
   3.13.2 protobuf-lite classes. The fields used are listed in
   [`polar_subset.proto`](polar-syncer/polar_subset.proto).
3. Sends the messages in order: `training-sessions/create`, then `exercises/create`,
   then `statistics`, `samples` and `route`, then `finalize`.

The session note is set to `Imported from Runtastic`, plus the weather if the export
recorded it.

## Convert to TCX

```sh
python3 -I polar-syncer/runtastic2tcx.py <export_dir> <out_dir>
```

The script writes one `.tcx` file per session and prints a table. For each file, the
table shows the sport, distance, number of track points, how many points got corrected
elevation, and how many got heart-rate data.

## Limitations

- **Supported sports.** `polar_upload.py` only handles running (`sport_type_id` 1) and
  cycling (3). Any other sport stops the run with `unknown Runtastic sport_type_id`. Use
  `--only` to work around those sessions. `runtastic2tcx.py` exports other sports as
  `Other`.
- **Manual entries.** Sessions without GPS data are uploaded with summary data only:
  duration, distance, calories and heart-rate averages. They have no route or samples.
- **Unofficial API.** The Polar Beat endpoints are not documented and could stop
  working at any time. The script logs in with the Polar Beat Android app's API
  credentials. Use it at your own risk.

## Privacy

A Runtastic export contains personal data, including GPS tracks, heart rate, photos and
account details. Don't commit it to a public repository. Add the export folders and
`polar-uploaded.json` to `.gitignore`, or keep the export outside the repository and pass
its path as `<export_dir>`.
