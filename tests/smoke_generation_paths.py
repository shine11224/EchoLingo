from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class FakeResponse:
    def __init__(
        self,
        *,
        json_data: dict | None = None,
        text: str = "",
        content: bytes = b"",
        url: str = "",
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data
        self.text = text
        self.content = content
        self.url = url
        self.status_code = status_code

    def json(self) -> dict:
        if self._json_data is None:
            raise ValueError("No JSON payload configured for fake response")
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1024):
        yield self.content or b"fake media bytes"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class OfflineRequests:
    bvid = "BV1SmokeTest1"
    aid = 123456
    cid = 654321

    @classmethod
    def get(cls, url: str, *args, **kwargs) -> FakeResponse:
        if "b23.tv" in url:
            return FakeResponse(url=f"https://www.bilibili.com/video/{cls.bvid}/?p=1")

        if "x/web-interface/view" in url:
            return FakeResponse(json_data={
                "code": 0,
                "data": {
                    "aid": cls.aid,
                    "cid": cls.cid,
                    "title": "Smoke Bilibili Lesson",
                    "pages": [
                        {"page": 1, "cid": cls.cid, "part": "Part One", "duration": 10},
                    ],
                },
            })

        if "x/player/v2" in url:
            return FakeResponse(json_data={
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {"lan": "en", "subtitle_url": "https://fake.local/en.json"},
                            {"lan": "zh-CN", "subtitle_url": "https://fake.local/zh.json"},
                        ],
                    },
                },
            })

        if url == "https://fake.local/en.json":
            return FakeResponse(json_data={"body": [
                {"from": 0.0, "to": 1.8, "content": "This is a smoke test."},
                {"from": 2.2, "to": 4.0, "content": "It checks the Bilibili path."},
            ]})

        if url == "https://fake.local/zh.json":
            return FakeResponse(json_data={"body": [
                {"from": 0.0, "to": 1.8, "content": "这是一个冒烟测试。"},
                {"from": 2.2, "to": 4.0, "content": "它检查 B 站路径。"},
            ]})

        if "article.local" in url:
            return FakeResponse(text="""
                <html>
                  <head><title>Smoke Article Lesson</title></head>
                  <body><main>
                    <p>This article path is covered.</p>
                    <p>It renders a lesson without using the network.</p>
                  </main></body>
                </html>
            """)

        raise RuntimeError(f"Unexpected network call in smoke test: {url}")


@contextlib.contextmanager
def patched_offline_sources(tmpdir: Path):
    import sources.bilibili as bilibili

    fake_audio = tmpdir / "fake-bilibili-audio.m4a"
    fake_audio.write_bytes(b"fake audio")

    with patch("requests.get", side_effect=OfflineRequests.get), \
            patch.object(bilibili, "_download_audio", return_value=fake_audio), \
            patch.object(bilibili, "_download_video", return_value=fake_audio):
        yield


def run_main(args: list[str]) -> Path:
    import main as app_main

    before = set(Path(args[args.index("--output-dir") + 1]).glob("*.html"))
    argv = ["main.py", *args]
    with patch.object(sys, "argv", argv):
        rc = app_main.main()
    if rc != 0:
        raise AssertionError(f"main.main returned {rc}: {' '.join(args)}")

    output_dir = Path(args[args.index("--output-dir") + 1])
    created = sorted(set(output_dir.glob("*.html")) - before, key=lambda p: p.stat().st_mtime)
    if not created:
        raise AssertionError(f"No HTML generated for args: {' '.join(args)}")
    return created[-1]


def assert_lesson_html(path: Path, expected_title: str, expected_source: str) -> None:
    raw = path.read_text(encoding="utf-8")
    required = [
        expected_title,
        f'const sourceType = "{expected_source}"',
        "const segments",
        "const analyses",
        "Quick review",
        "copyQuickReviewMarkdown",
        "smoke",
    ]
    missing = [item for item in required if item not in raw]
    if missing:
        raise AssertionError(f"{path} is missing expected markers: {missing}")


def make_local_fixture(tmpdir: Path) -> tuple[Path, Path]:
    media = tmpdir / "local-smoke.mp3"
    media.write_bytes(b"fake local audio")
    subtitle = tmpdir / "local-smoke.vtt"
    subtitle.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.800\n"
        "This local file path is covered.\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "The transcript prevents Whisper from running.\n",
        encoding="utf-8",
    )
    return media, subtitle


def smoke_success_paths() -> list[tuple[str, Path]]:
    results: list[tuple[str, Path]] = []
    with tempfile.TemporaryDirectory(prefix="english-tool-smoke-") as tmp:
        tmpdir = Path(tmp)
        output_dir = tmpdir / "out"
        media, subtitle = make_local_fixture(tmpdir)

        common = ["--analysis-mode", "mock", "--output-dir", str(output_dir)]
        with patched_offline_sources(tmpdir):
            cases = [
                (
                    "bilibili-normal",
                    ["--bilibili", f"https://www.bilibili.com/video/{OfflineRequests.bvid}", *common],
                    "Smoke Bilibili Lesson",
                    "local_video",
                ),
                (
                    "bilibili-short",
                    ["--bilibili", "https://b23.tv/smoke", *common],
                    "Smoke Bilibili Lesson",
                    "local_video",
                ),
                (
                    "article",
                    ["--article", "https://article.local/smoke", *common],
                    "Smoke Article Lesson",
                    "article",
                ),
                (
                    "local-file-with-subtitle",
                    ["--video-file", str(media), "--transcript-file", str(subtitle), *common],
                    "local-smoke",
                    "local_video",
                ),
            ]
            for name, args, title, source_type in cases:
                html = run_main(args)
                assert_lesson_html(html, title, source_type)
                results.append((name, html))

    return results


def assert_cli_accepts_groq_whisper() -> None:
    import main as app_main

    args = app_main.build_parser().parse_args([
        "--bilibili",
        "https://www.bilibili.com/video/BV1SmokeTest1",
        "--analysis-mode",
        "mock",
        "--whisper-model",
        "groq",
    ])
    if args.whisper_model != "groq":
        raise AssertionError("CLI did not preserve --whisper-model groq")


def failure_checklist() -> list[str]:
    return [
        "Missing DeepSeek key: run generation with --analysis-mode mock for offline QA; for deepseek mode, /health should show deepseek_key=false and the UI/API should surface a missing-key action.",
        "Missing transcript dependency path: run local media without --transcript-file in an environment without faster-whisper; expected failure should mention installing faster-whisper or passing --transcript-file.",
        "Missing ffmpeg: run local video without transcript after removing ffmpeg from PATH; expected failure should identify ffmpeg instead of a generic subprocess exit.",
        "Bilibili auth/cookies required: fake or observe playurl code != 0; expected message should tell the user to export cookies.txt and pass --bilibili-cookies.",
        "Network failure: patch requests.get to raise Timeout/ConnectionError for article and Bilibili metadata; expected result should preserve the failing source step and original URL.",
        "API quota/rate limit: force Groq/DeepSeek 429; expected behavior is fallback where supported, otherwise an actionable quota/rate-limit message.",
        "Bad AI JSON: force DeepSeek to return invalid JSON in analyze/resegment paths; expected behavior is retry/repair or a clear JSON parse failure tied to the AI step.",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline smoke test for core generation paths.")
    parser.add_argument("--list-failure-checks", action="store_true", help="Print failure scenario checklist and exit.")
    args = parser.parse_args()

    if args.list_failure_checks:
        for item in failure_checklist():
            print(f"- {item}")
        return 0

    print("Running offline generation smoke tests...")
    assert_cli_accepts_groq_whisper()
    print("PASS cli-groq-whisper")
    results = smoke_success_paths()
    for name, html in results:
        print(f"PASS {name}: {html}")

    print("\nFailure scenario checklist:")
    for item in failure_checklist():
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
