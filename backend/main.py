from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from analyzer import SentenceAnalyzer
from renderer import LessonRenderer
from sources import (
    build_article_lesson,
    build_bilibili_lesson,
    build_local_video_lesson,
    build_youtube_lesson,
)
from sources.bilibili import probe_bilibili

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate interactive English listening lessons.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--youtube", help="YouTube video URL")
    source_group.add_argument("--bilibili", help="Bilibili video URL")
    source_group.add_argument("--article", help="Article URL")
    source_group.add_argument("--video-file", help="Local video file path")
    source_group.add_argument("--text-file", help="MD or TXT file path for text-based lesson (Phase A skipped, no audio)")
    parser.add_argument("--transcript-file", help="Optional subtitle/transcript file for local video")
    parser.add_argument("--text-title", default="", help="Override title for text-file lesson")
    parser.add_argument("--youtube-cookies", help="Optional Netscape cookies.txt for YouTube subtitle fetching")
    parser.add_argument("--bilibili-cookies", help="Optional Netscape cookies.txt for Bilibili (needed when playurl requires login)")
    parser.add_argument("--bilibili-page", type=int, help="Bilibili page number for multi-part videos; equivalent to ?p=N")
    parser.add_argument(
        "--bilibili-download",
        choices=["audio", "video"],
        default="audio",
        help="Download audio only by default, or full video when needed.",
    )
    parser.add_argument(
        "--bilibili-interactive",
        action="store_true",
        help="Prompt for Bilibili page, download mode, and Whisper model.",
    )
    parser.add_argument(
        "--analysis-mode",
        choices=["auto", "deepseek", "mock"],
        default="auto",
        help="Use DeepSeek API when available (auto), force deepseek, or use local fallback (mock).",
    )
    parser.add_argument(
        "--whisper-model",
        choices=["base", "medium", "large-v3", "groq"],
        default="large-v3",
        help="Transcription backend/model: groq (API), base (fastest), medium (balanced), large-v3 (best accuracy, default)",
    )
    parser.add_argument(
        "--youtube-download",
        choices=["subtitle-only", "audio"],
        default="subtitle-only",
        help="subtitle-only: embed YouTube iframe player; audio: download audio-only for blind listening.",
    )
    parser.add_argument("--loop-count", type=int, default=3, help="Default loop count for each sentence")
    parser.add_argument("--disable-ai-segmentation", action="store_true", help="Disable AI boundary review")
    parser.add_argument("--disable-ai-ipa", action="store_true", help="Disable AI IPA refinement")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Directory for generated HTML files")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.disable_ai_segmentation:
        os.environ["ELT_ENABLE_AI_BOUNDARY_REVIEW"] = "0"
    if args.disable_ai_ipa:
        os.environ["ELT_ENABLE_AI_IPA_REFINEMENT"] = "0"
    if args.youtube:
        print("[STEP:download]")
        source = build_youtube_lesson(
            args.youtube,
            cookies_file=args.youtube_cookies,
            download_audio=(args.youtube_download == "audio"),
        )
        print("[STEP:subtitle]")
    elif args.bilibili:
        bilibili_url, download_video, whisper_model = _resolve_bilibili_episode(
            args.bilibili,
            args.bilibili_cookies,
            page=args.bilibili_page,
            download_video=args.bilibili_download == "video",
            whisper_model=args.whisper_model,
            interactive=args.bilibili_interactive,
        )
        source = build_bilibili_lesson(bilibili_url, cookies_file=args.bilibili_cookies, download_video=download_video, whisper_model=whisper_model)
    elif args.text_file:
        from sources.text import build_text_lesson
        source = build_text_lesson(args.text_file, title=getattr(args, "text_title", "") or "")
    elif args.article:
        source = build_article_lesson(args.article)
    else:
        source = build_local_video_lesson(args.video_file, args.transcript_file, whisper_model=args.whisper_model)

    analyses: list = []

    if args.analysis_mode == "mock":
        print("[STEP:resegment_translate]")
        print("  [resegment] mock 模式，跳过重新断句和翻译")
    else:
        missing = sum(1 for s in source.segments if not s.translation)
        if missing:
            analyzer = SentenceAnalyzer(mode=args.analysis_mode)
            import datetime as _dt
            _trace_dir = Path(args.output_dir) / "pipeline_traces" / _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            print("[STEP:resegment_translate]")
            print(f"[analyzer] {missing} 个句子缺少中文，一步完成：合并断句 + 翻译 + 分析…")
            source.segments, analyses = analyzer.analyze_from_raw_segments(source.segments, source.title, trace_dir=_trace_dir)
        else:
            print("[STEP:resegment_translate]")
            print("  [翻译] 已有中文翻译，跳过翻译与重断句")

    if not analyses:
        analyzer = SentenceAnalyzer(mode=args.analysis_mode)
        print("[STEP:analyze]")
        analyses = analyzer.analyze(source.segments, source.title)
    renderer = LessonRenderer(BASE_DIR.parent / "frontend" / "templates")
    output_path = renderer.render(source, analyses, Path(args.output_dir), loop_count=args.loop_count)

    print(f"Generated lesson: {output_path}")
    return 0


def _resolve_bilibili_episode(
    url: str,
    cookies_file: str | None,
    page: int | None = None,
    download_video: bool = False,
    whisper_model: str = "large-v3",
    interactive: bool = False,
) -> tuple[str, bool, str]:
    """Resolve Bilibili page/download settings without blocking automation by default."""
    from urllib.parse import parse_qs, urlparse

    if not interactive:
        final_url = _with_bilibili_page(url, page) if page else url
        return final_url, download_video, whisper_model

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    already_has_p = "p" in qs

    # ── 步骤1：获取视频信息（含字幕检测）──────────────────
    print("正在获取视频信息...")
    info = probe_bilibili(url, cookies_file, check_subtitles=True)
    pages = info["pages"]
    total = len(pages)
    title = info["title"]
    has_subtitle = info.get("has_subtitle", False)

    # ── 步骤2：选集数（多集且未指定时） ────────────────────
    if info["is_multi"] and not already_has_p:
        print(f"\n《{title}》共 {total} 集：\n")
        display_pages = pages[:30]
        for i in range(0, len(display_pages), 3):
            row = display_pages[i:i + 3]
            line = "  ".join(
                f"p{p['page']:>3}. {p['part'][:28]:<28} ({p['duration']//60}:{p['duration']%60:02d})"
                for p in row
            )
            print(f"  {line}")
        if total > 30:
            print(f"  ... 还有 {total - 30} 集，可直接输入集数（1~{total}）")
        print()
        while True:
            raw = input(f"请输入要学习的集数（1~{total}，直接回车默认第 1 集）：").strip()
            if raw == "":
                part_index = 1
                break
            if raw.isdigit() and 1 <= int(raw) <= total:
                part_index = int(raw)
                break
            print(f"  请输入 1 到 {total} 之间的整数。")
        selected = next(p for p in pages if p["page"] == part_index)
        print(f"  已选择：p{part_index} — {selected['part']}")
        final_url = f"https://www.bilibili.com/video/{info['bvid']}/?p={part_index}"
        # 更新字幕检测（已切换集数，重新探测）
        info2 = probe_bilibili(final_url, cookies_file, check_subtitles=True)
        has_subtitle = info2.get("has_subtitle", False)
    else:
        final_url = url

    # ── 步骤3：选择下载模式 ─────────────────────────────
    print()
    print("下载模式：")
    print("  [1] 仅音频（默认，更快，约 5~30 MB）")
    print("  [2] 完整视频（较慢，约 100~500 MB）")
    while True:
        raw = input("请选择（直接回车默认音频）：").strip()
        if raw in ("", "1"):
            download_video = False
            print("  已选择：仅音频")
            break
        if raw == "2":
            download_video = True
            print("  已选择：完整视频")
            break
        print("  请输入 1 或 2。")

    # ── 步骤4：Whisper 精度（仅无字幕时出现）──────────────
    if not has_subtitle:
        # 估算音频时长（用第一集 duration 作参考）
        ref_duration = pages[0]["duration"] if pages else 600
        print()
        print("  未检测到英文字幕，需要本地 Whisper 转录。")
        print(f"  预计音频时长约 {ref_duration//60} 分钟，请选择转录精度：\n")
        print("  [1] 快速   base    (~150 MB) — 约 {:.0f} 分钟，适合快速预览".format(ref_duration / 60 * 0.3))
        print("  [2] 平衡   medium  (~1.5 GB) — 约 {:.0f} 分钟，日常推荐".format(ref_duration / 60 * 0.8))
        print("  [3] 精准   large-v3 (~3 GB)  — 约 {:.0f} 分钟，精听材料推荐（默认）".format(ref_duration / 60 * 2.0))
        print()
        while True:
            raw = input("请选择（直接回车默认精准）：").strip()
            if raw in ("", "3"):
                whisper_model = "large-v3"
                print("  已选择：精准模式 large-v3")
                break
            if raw == "1":
                whisper_model = "base"
                print("  已选择：快速模式 base")
                break
            if raw == "2":
                whisper_model = "medium"
                print("  已选择：平衡模式 medium")
                break
            print("  请输入 1、2 或 3。")
    else:
        print(f"\n  已检测到英文字幕，将直接使用，无需 Whisper 转录。")

    print()
    return final_url, download_video, whisper_model


def _with_bilibili_page(url: str, page: int) -> str:
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    if page < 1:
        raise ValueError("--bilibili-page must be >= 1")

    if url.startswith("BV"):
        return f"https://www.bilibili.com/video/{url.split('?')[0]}/?p={page}"

    parsed = urlparse(url)
    query = dict(parse_qs(parsed.query))
    query["p"] = [str(page)]
    encoded = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=encoded))


if __name__ == "__main__":
    raise SystemExit(main())
