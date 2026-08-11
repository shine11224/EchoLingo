from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke checks for the lesson generation pipeline.")
    parser.add_argument("--server", default="http://localhost:5173", help="Running Flask server URL.")
    parser.add_argument("--skip-server", action="store_true", help="Skip API checks against Flask.")
    parser.add_argument("--skip-local-cli", action="store_true", help="Skip local file + transcript CLI smoke.")
    parser.add_argument("--test-client", action="store_true", help="Check Flask routes in-process instead of localhost.")
    parser.add_argument("--live-submit", action="store_true", help="Submit live URL jobs from SMOKE_* env vars.")
    parser.add_argument("--wait-seconds", type=int, default=0, help="Optional seconds to poll live jobs.")
    args = parser.parse_args()

    failures: list[str] = []
    if not args.skip_server:
        failures += check_test_client() if args.test_client else check_server(args.server.rstrip("/"))
        if args.live_submit:
            failures += submit_live_matrix(args.server.rstrip("/"), args.wait_seconds)
        else:
            print("[skip] live URL submissions disabled. Use --live-submit with SMOKE_* env vars.")
    if not args.skip_local_cli:
        failures += check_local_cli()

    if failures:
        print("\n[failed]")
        for item in failures:
            print(f"- {item}")
        return 1
    print("\n[ok] smoke checks passed")
    return 0


def check_server(server: str) -> list[str]:
    failures: list[str] = []
    print(f"[server] checking {server}")

    health = get_json(f"{server}/health")
    if health is None:
        failures.append("/health did not return JSON")
    else:
        env = health.get("environment") or {}
        if env:
            required = ["python", "ffmpeg", "sqlite", "keys"]
            missing = [key for key in required if key not in env]
            if missing:
                failures.append(f"/health environment missing sections: {missing}")
            else:
                print("[server] environment diagnostics ok")

    schema = get_json(f"{server}/api/pipeline/schema")
    steps = (schema or {}).get("steps") or []
    expected = ["init", "download", "subtitle", "whisper_load", "whisper_transcribe", "resegment_translate", "analyze", "render"]
    got = [s.get("id") for s in steps]
    if got != expected:
        failures.append(f"pipeline schema mismatch: {got}")
    else:
        print("[server] pipeline schema ok")

    missing = post_json(f"{server}/api/generate", {"source_type": "bilibili", "url": ""})
    code = ((missing or {}).get("error_info") or {}).get("code")
    if code != "URL_REQUIRED":
        failures.append(f"missing URL error_info.code expected URL_REQUIRED, got {code!r}")
    else:
        print("[server] URL_REQUIRED error shape ok")

    unsupported = post_json(f"{server}/api/generate", {"source_type": "unknown", "url": "x"})
    code = ((unsupported or {}).get("error_info") or {}).get("code")
    if code != "UNSUPPORTED_SOURCE":
        failures.append(f"unsupported source error_info.code expected UNSUPPORTED_SOURCE, got {code!r}")
    else:
        print("[server] UNSUPPORTED_SOURCE error shape ok")

    local_missing = post_json(f"{server}/api/generate", {"source_type": "local", "url": ""})
    code = ((local_missing or {}).get("error_info") or {}).get("code")
    if code != "LOCAL_PATH_REQUIRED":
        failures.append(f"local path error_info.code expected LOCAL_PATH_REQUIRED, got {code!r}")
    else:
        print("[server] LOCAL_PATH_REQUIRED error shape ok")

    return failures


def check_test_client() -> list[str]:
    failures: list[str] = []
    print("[server] checking Flask test client", flush=True)
    sys.path.insert(0, str(ROOT / "backend"))
    from server import app

    client = app.test_client()
    health = client.get("/health").get_json() or {}
    env = health.get("environment") or {}
    required = ["python", "ffmpeg", "sqlite", "keys"]
    missing = [key for key in required if key not in env]
    if missing:
        failures.append(f"/health environment missing sections: {missing}")
    else:
        print("[server] environment diagnostics ok")

    schema = client.get("/api/pipeline/schema").get_json() or {}
    expected = ["init", "download", "subtitle", "whisper_load", "whisper_transcribe", "resegment_translate", "analyze", "render"]
    got = [s.get("id") for s in schema.get("steps", [])]
    if got != expected:
        failures.append(f"pipeline schema mismatch: {got}")
    else:
        print("[server] pipeline schema ok")

    cases = [
        ("URL_REQUIRED", {"source_type": "bilibili", "url": ""}),
        ("UNSUPPORTED_SOURCE", {"source_type": "unknown", "url": "x"}),
        ("LOCAL_PATH_REQUIRED", {"source_type": "local", "url": ""}),
    ]
    for expected_code, payload in cases:
        data = client.post("/api/generate", json=payload).get_json() or {}
        code = (data.get("error_info") or {}).get("code")
        if code != expected_code:
            failures.append(f"{expected_code} error_info.code mismatch: {code!r}")
        else:
            print(f"[server] {expected_code} error shape ok")
    return failures


def check_local_cli() -> list[str]:
    print("[cli] local file + transcript smoke")
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="elt-smoke-") as tmp:
        tmpdir = Path(tmp)
        media = tmpdir / "sample.mp4"
        vtt = tmpdir / "sample.vtt"
        out = tmpdir / "output"
        media.write_bytes(b"")
        vtt.write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "Hello world.\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "This is a smoke test.\n",
            encoding="utf-8",
        )
        cmd = [
            PYTHON,
            str(ROOT / "backend" / "main.py"),
            "--video-file",
            str(media),
            "--transcript-file",
            str(vtt),
            "--analysis-mode",
            "mock",
            "--output-dir",
            str(out),
        ]
        proc = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
        if proc.returncode != 0:
            failures.append(f"local CLI smoke failed: {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
            return failures
        generated = list(out.glob("*.html"))
        if not generated:
            failures.append("local CLI smoke did not generate HTML")
        else:
            print(f"[cli] generated {generated[0].name}")
    return failures


def submit_live_matrix(server: str, wait_seconds: int) -> list[str]:
    matrix = [
        ("bilibili", "SMOKE_BILIBILI_URL", {"source_type": "bilibili"}),
        ("b23", "SMOKE_B23_URL", {"source_type": "bilibili"}),
        ("article", "SMOKE_ARTICLE_URL", {"source_type": "article"}),
        ("local", "SMOKE_LOCAL_VIDEO", {"source_type": "local", "transcript_path": os.environ.get("SMOKE_LOCAL_TRANSCRIPT", "")}),
    ]
    failures: list[str] = []
    for label, env_name, payload in matrix:
        value = os.environ.get(env_name, "").strip()
        if not value:
            print(f"[skip] {label}: set {env_name} to submit this path")
            continue
        payload = {**payload, "url": value, "analysis_mode": "mock", "whisper_model": "base"}
        data = post_json(f"{server}/api/generate", payload)
        job_id = (data or {}).get("job_id")
        if not job_id:
            failures.append(f"{label} submit failed: {data}")
            continue
        print(f"[live] {label} job {job_id}")
        if wait_seconds:
            failures += poll_job(server, job_id, wait_seconds)
    return failures


def poll_job(server: str, job_id: str, wait_seconds: int) -> list[str]:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        data = get_json(f"{server}/api/generate/status/{job_id}") or {}
        status = data.get("status")
        if status == "done":
            print(f"[live] {job_id} done: {data.get('output_file')}")
            return []
        if status == "error":
            return [f"{job_id} failed: {data.get('error_info') or data.get('error')}"]
        time.sleep(2)
    print(f"[live] {job_id} still running after {wait_seconds}s")
    return []


def get_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[warn] GET {url}: {exc}")
        return None


def post_json(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except json.JSONDecodeError:
            return {"error": str(exc)}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
