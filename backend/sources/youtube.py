from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from schemas import Segment, SourceBundle
from sources.subtitle_parser import parse_subtitle_file

_BASE_DIR = Path(__file__).resolve().parents[2]  # repo 根目录（backend/sources/ 上两级）


def _yt_proxy() -> str | None:
    """Optional proxy for all YouTube traffic — used on cloud servers whose
    datacenter IP is bot-checked by YouTube. Set YOUTUBE_PROXY, e.g.
    http://mihomo:7890 (compose service name)."""
    import os

    return os.environ.get("YOUTUBE_PROXY", "").strip() or None


def _default_cookies_file() -> str | None:
    """Pick up a Netscape cookies.txt placed by the user/deploy, so the web flow
    gets YouTube cookies without a CLI flag. Priority: explicit env var, then
    $ELT_CONFIG_DIR (cloud config volume), then repo resources/."""
    import os

    candidates: list[Path] = []
    env_file = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if env_file:
        candidates.append(Path(env_file))
    config_dir = os.environ.get("ELT_CONFIG_DIR", "").strip()
    if config_dir:
        candidates.append(Path(config_dir) / "youtube_cookies.txt")
    candidates.append(_BASE_DIR / "resources" / "youtube_cookies.txt")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def build_youtube_lesson(
    url: str,
    cookies_file: str | None = None,
    download_audio: bool = False,
) -> SourceBundle:
    cookies_file = cookies_file or _default_cookies_file()
    video_id = extract_video_id(url)
    try:
        bundle = _build_with_ytdlp(url, video_id, cookies_file)
    except Exception as first_error:
        try:
            bundle = _build_with_transcript_api(url, video_id)
        except Exception as second_error:
            raise RuntimeError(
                "YouTube 字幕获取失败：yt-dlp 和 youtube-transcript-api 都未成功。"
                "如果这是受限视频，请尝试更换公开视频，或传入 --youtube-cookies。"
            ) from second_error

    if download_audio:
        cache_dir = _BASE_DIR / ".cache" / "youtube" / video_id
        audio_path = _download_audio(url, video_id, cache_dir, cookies_file)
        bundle = SourceBundle(
            source_type="local_video",
            title=bundle.title,
            source_value=bundle.source_value,
            segments=bundle.segments,
            youtube_id=bundle.youtube_id,
            local_video=audio_path,
        )

    return bundle


def _download_audio(url: str, video_id: str, cache_dir: Path, cookies_file: str | None) -> Path:
    """Download audio-only stream via yt-dlp, cache to avoid re-download."""
    from yt_dlp import YoutubeDL

    cache_dir.mkdir(parents=True, exist_ok=True)
    audio_path = cache_dir / f"{video_id}.m4a"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        print(f"  复用已下载音频：{audio_path}", flush=True)
        return audio_path

    print("  下载 YouTube 音频中…", flush=True)
    import sys, pathlib
    _conda_ffmpeg = pathlib.Path(sys.executable).parent / "Library" / "bin"
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(cache_dir / f"{video_id}.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "128"}],
        "quiet": True,
        "no_warnings": True,
    }
    if (_conda_ffmpeg / "ffmpeg.exe").exists():
        opts["ffmpeg_location"] = str(_conda_ffmpeg)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if proxy := _yt_proxy():
        opts["proxy"] = proxy
    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    if not audio_path.exists():
        candidates = list(cache_dir.glob(f"{video_id}.*"))
        if candidates:
            candidates[0].rename(audio_path)
        else:
            raise RuntimeError("YouTube 音频下载失败，未找到输出文件。")
    return audio_path


def download_youtube_audio(url: str, cookies_file: str | None = None) -> Path:
    """Download or reuse the audio stream for background alignment."""
    video_id = extract_video_id(url)
    cache_dir = _BASE_DIR / ".cache" / "youtube" / video_id
    return _download_audio(url, video_id, cache_dir, cookies_file)


def _build_with_ytdlp(url: str, video_id: str, cookies_file: str | None = None) -> SourceBundle:
    from yt_dlp import YoutubeDL

    cache_dir = _BASE_DIR / ".cache" / "youtube" / video_id
    info_cache = cache_dir / "info.json"
    cached_vtt = _find_subtitle(cache_dir, video_id) if cache_dir.exists() else None

    if cached_vtt and info_cache.exists():
        print(f"  复用已缓存字幕：{cached_vtt}", flush=True)
        subtitle_path = cached_vtt
        title = json.loads(info_cache.read_text(encoding="utf-8")).get("title", video_id)
    else:
        with tempfile.TemporaryDirectory(prefix="english-lesson-") as tmpdir:
            tmp_path = Path(tmpdir)
            options = {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en.*", "en"],
                "subtitlesformat": "vtt",
                "outtmpl": str(tmp_path / "%(id)s.%(ext)s"),
            }
            if cookies_file:
                options["cookiefile"] = cookies_file
            if proxy := _yt_proxy():
                options["proxy"] = proxy
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

            tmp_vtt = _find_subtitle(tmp_path, info["id"])
            if not tmp_vtt:
                raise RuntimeError("未找到英文字幕，无法生成精听页面。")

            cache_dir.mkdir(parents=True, exist_ok=True)
            subtitle_path = cache_dir / tmp_vtt.name
            shutil.copy(tmp_vtt, subtitle_path)
            title = info.get("title", info["id"])
            info_cache.write_text(
                json.dumps({"title": title, "id": video_id}, ensure_ascii=False),
                encoding="utf-8",
            )

    segments = parse_subtitle_file(subtitle_path, debug_dir=cache_dir)
    if not segments:
        raise RuntimeError("字幕解析失败，未得到可用句子。")

    return SourceBundle(
        source_type="youtube",
        title=title,
        source_value=url,
        segments=segments,
        youtube_id=video_id,
    )


def _build_with_transcript_api(url: str, video_id: str) -> SourceBundle:
    from youtube_transcript_api import YouTubeTranscriptApi

    if proxy := _yt_proxy():
        from youtube_transcript_api.proxies import GenericProxyConfig

        api = YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy)
        )
    else:
        api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    segments = [
        Segment(
            index=i + 1,
            text=item.text.strip(),
            start=float(item.start),
            end=float(item.start + item.duration),
        )
        for i, item in enumerate(transcript)
        if item.text.strip()
    ]
    merged = _merge_short_segments(segments)
    return SourceBundle(
        source_type="youtube",
        title=f"YouTube Lesson {video_id}",
        source_value=url,
        segments=merged,
        youtube_id=video_id,
    )


def _find_subtitle(tmp_path: Path, video_id: str) -> Path | None:
    candidates = sorted(tmp_path.glob(f"{video_id}*.vtt"))
    for candidate in candidates:
        if ".en" in candidate.name:
            return candidate
    return candidates[0] if candidates else None


def extract_video_id(url: str) -> str:
    patterns = [
        r"v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"embed/([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    raise ValueError(f"无法从链接中解析 YouTube video id: {url}")


_extract_video_id = extract_video_id  # legacy alias


def source_bundle_to_segment_dicts(bundle: SourceBundle) -> list[dict]:
    return [
        {
            "index": s.index, "start": s.start, "end": s.end, "text": s.text,
            **({"words": s.words} if getattr(s, "words", None) else {}),
        }
        for s in bundle.segments
    ]


def fetch_youtube_subtitles(url: str, cookies_file: str | None = None) -> SourceBundle:
    return build_youtube_lesson(url, cookies_file=cookies_file, download_audio=False)


def _merge_short_segments(items: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    bucket: list[Segment] = []
    index = 1
    for segment in items:
        bucket.append(segment)
        combined = " ".join(item.text for item in bucket)
        if re.search(r"[.!?][\"']?$", segment.text) or len(combined.split()) >= 18:
            merged.append(
                Segment(
                    index=index,
                    text=combined,
                    start=bucket[0].start,
                    end=bucket[-1].end,
                )
            )
            bucket = []
            index += 1
    if bucket:
        merged.append(
            Segment(
                index=index,
                text=" ".join(item.text for item in bucket),
                start=bucket[0].start,
                end=bucket[-1].end,
            )
        )
    return merged
