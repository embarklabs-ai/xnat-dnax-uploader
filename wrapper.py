#!/usr/bin/env python3
"""
XNAT session-level container that uploads
processed data (csv) from a session's RESOURCES to a
DNAnexus project at /<subject_label>/<session_label>/processed_data/.

Every run writes a CSV manifest to /output (success OR failure), one
header row plus one data row. XNAT CS uploads /output into the source
project's `xnat-to-dnax-transfer-log` resource, giving a project-level
audit log across every session run.

Runtime inputs (all populated by XNAT Container Service):

  Env vars:
    DX_AUTH_TOKEN   Injected from a CS secret 
    SUBJECT_LABEL   Derived from the session's Subject.
    SESSION_LABEL   Derived from the session.

  Argv:
    --dest-project  DNAnexus destination project ID (user input at launch).

Container exit strategy:
    Wrapper exit 0  =>  success OR one of the expected data failures below;
                        in both cases the manifest CSV is uploaded to
                        `xnat-to-dnax-transfer-log`.
    Wrapper exit 2  =>  hard config error (env var missing).

Manifest `error_code` values on failure (all exit wrapper 0):
    3  zero or more than one matching CSV found
    4  ua (upload-agent) exited non-zero
    5  ua (upload-agent) succeeded but no DNAnexus file-id parsed from its output
    6  invalid --dest-project format
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CONTAINER_VERSION = "1.0.1"
INPUT_ROOT = Path("/input")
OUTPUT_ROOT = Path("/output")
TARGET_FILENAME = "lesion_mask_radiomic_features.csv"
UA_BIN = "/usr/local/bin/ua"

TOKEN_REDACT = re.compile(r"SECURITY_CONTEXT|auth[-_]?token|Bearer", re.IGNORECASE)
FILE_ID_RE = re.compile(r"\b(file-[A-Za-z0-9]{20,})\b")
PROJECT_ID_RE = re.compile(r"^project-[A-Za-z0-9]+$")

MANIFEST_FIELDS = [
    "timestamp_utc",
    "container_version",
    "status",
    "source_path",
    "bytes",
    "subject_label",
    "session_label",
    "dnax_project_id",
    "dnax_folder",
    "dnax_filename",
    "dnax_file_id",
    "error_code",
    "error_message",
]

# Accumulates as the run progresses. On failure at any step, die() flushes
# whatever's been resolved so far into a manifest row.
STATE = {}


def log(msg):
    print(f"[uploader {CONTAINER_VERSION}] {msg}", flush=True)


def _write_manifest(row):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    session_for_name = STATE.get("session_label") or "unknown-session"
    path = OUTPUT_ROOT / f"{session_for_name}-{ts}.csv"
    row.setdefault("timestamp_utc", now.isoformat())
    row.setdefault("container_version", CONTAINER_VERSION)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerow(row)
    log(f"manifest -> {path}")


def die(code, msg):
    print(f"[uploader {CONTAINER_VERSION}] ERROR: {msg}", file=sys.stderr, flush=True)

    # Config errors (missing env var) => exit non-zero 
    if code == 2:
        sys.exit(code)

    # Expected data errors => write a status=failure manifest, and
    # exit 0 so CS finalization uploads error log /output.
    try:
        _write_manifest({
            "status": "failure",
            "source_path": STATE.get("source_path", ""),
            "bytes": STATE.get("bytes", ""),
            "subject_label": STATE.get("subject_label", ""),
            "session_label": STATE.get("session_label", ""),
            "dnax_project_id": STATE.get("dnax_project_id", ""),
            "dnax_folder": STATE.get("dnax_folder", ""),
            "dnax_filename": STATE.get("dnax_filename", ""),
            "dnax_file_id": STATE.get("dnax_file_id", ""),
            "error_code": str(code),
            "error_message": msg,
        })
    except Exception as e:
        print(
            f"[uploader {CONTAINER_VERSION}] additionally failed writing failure manifest: {e}",
            file=sys.stderr, flush=True,
        )

    banner = f"===== UPLOAD FAILED (error_code={code}): {msg} ====="
    bar = "=" * len(banner)
    print(bar, flush=True)
    print(banner, flush=True)
    print(bar, flush=True)
    sys.exit(0)


def require_env(name):
    v = os.environ.get(name)
    if not v:
        die(2, f"required env var {name} is missing or empty")
    return v


def find_csv():
    matches = sorted(p for p in INPUT_ROOT.rglob(TARGET_FILENAME) if p.is_file())
    if len(matches) == 1:
        return matches[0]

    detail = f"expected exactly 1 file named {TARGET_FILENAME}, found {len(matches)}"
    log(detail)
    for p in matches:
        log(f"  match: {p}")
    resources = INPUT_ROOT / "RESOURCES"
    if resources.is_dir():
        log("directory listing of /input/RESOURCES for debugging:")
        for p in sorted(resources.rglob("*")):
            log(f"  {p.relative_to(INPUT_ROOT)}")
    else:
        log("/input/RESOURCES folder is not present")
    die(3, detail)


def emit_filtered(stream_text, prefix):
    for line in (stream_text or "").splitlines():
        if not TOKEN_REDACT.search(line):
            print(f"{prefix} {line}", flush=True)


def upload(csv_path, project_id, folder, token):
    # Stage the CSV at a per-session unique local path before calling `ua`.
    # DNAnexus / ua deduplicates by `.system-fileSignature` (size + mtime +
    # source_path). Every container mounts /input identically, so if two
    # sessions' CSVs share size + mtime, their signatures collide and one
    # session's bytes are silently dropped. Copying to a session-labeled
    # /tmp path (and using shutil.copy so mtime is reset to "now") makes
    # each upload's signature unique. Manifest still records the original
    # /input path in source_path for traceability.
    staged_dir = Path("/tmp/dnax_upload") / STATE["session_label"]
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staged_dir / csv_path.name
    shutil.copy(csv_path, staged_path)
    log(f"staged for upload at {staged_path}")

    log(f"uploading {csv_path.name} -> {project_id}:{folder}/")
    cmd = [
        UA_BIN,
        "--auth-token", token,
        "--project", project_id,
        "--folder", folder,
        "--do-not-compress",
        "--wait-on-close",
        str(staged_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    emit_filtered(result.stdout, "ua>")
    emit_filtered(result.stderr, "ua!>")
    if result.returncode != 0:
        die(4, f"ua exited with code {result.returncode}")

    m = FILE_ID_RE.search(result.stdout or "")
    if not m:
        die(5, "ua exited 0 but no file-id was found in its stdout")
    return m.group(1)


def main():
    parser = argparse.ArgumentParser(
        description="Upload lesion_mask_radiomic_features.csv from an XNAT session to DNAnexus."
    )
    parser.add_argument(
        "-p", "--dest-project", required=True,
        help="DNAnexus destination project ID (project-XXXXXXXX...)",
    )
    args = parser.parse_args()

    # Read session identifiers FIRST so any subsequent failure can name
    # the manifest file with the real session label.
    STATE["subject_label"] = require_env("SUBJECT_LABEL")
    STATE["session_label"] = require_env("SESSION_LABEL")
    token = require_env("DX_AUTH_TOKEN")

    if not PROJECT_ID_RE.match(args.dest_project):
        die(6, f"invalid DNAnexus project ID format: {args.dest_project!r}")
    STATE["dnax_project_id"] = args.dest_project

    csv_path = find_csv()
    log(f"found csv: {csv_path}")
    STATE["source_path"] = str(csv_path)
    STATE["bytes"] = csv_path.stat().st_size

    folder = f"/{STATE['subject_label']}/{STATE['session_label']}/processed_data"
    STATE["dnax_folder"] = folder
    STATE["dnax_filename"] = csv_path.name

    file_id = upload(csv_path, args.dest_project, folder, token)
    STATE["dnax_file_id"] = file_id
    log(f"uploaded as {file_id}")

    _write_manifest({
        "status": "success",
        "source_path": STATE["source_path"],
        "bytes": STATE["bytes"],
        "subject_label": STATE["subject_label"],
        "session_label": STATE["session_label"],
        "dnax_project_id": STATE["dnax_project_id"],
        "dnax_folder": STATE["dnax_folder"],
        "dnax_filename": STATE["dnax_filename"],
        "dnax_file_id": STATE["dnax_file_id"],
        "error_code": "",
        "error_message": "",
    })
    log("done")


if __name__ == "__main__":
    main()
