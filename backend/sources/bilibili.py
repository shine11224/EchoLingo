from __future__ import annotations

import re
from pathlib import Path

import requests

from schemas import Segment, SourceBundle
from sources.baidu import build_local_video_lesson

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def probe_bilibili(url: str, cookies_file: str | None = None, check_subtitles: bool = False) -> dict:
    """只获取视频元信息，不下载任何内容。
    返回 {bvid, title, pages, is_multi, has_subtitle (仅 check_subtitles=True 时)}
    """
    cookies = _load_cookies_txt(cookies_file)
    bvid, part_index = _extract_bvid_and_part(url)
    view = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
    ).json()
    if view.get("code") != 0:
        raise RuntimeError(f"Bilibili view API 失败: {view}")
    data = view["data"]
    pages = [
        {"page": p["page"], "part": p.get("part", ""), "duration": p.get("duration", 0)}
        for p in (data.get("pages") or [])
    ]
    result = {
        "bvid": bvid,
        "title": data["title"],
        "pages": pages,
        "is_multi": len(pages) > 1,
    }

    if check_subtitles:
        # 用第一集（或指定集）的 cid 快速查字幕
        target_page = part_index or 1
        matched = next((p for p in pages if p["page"] == target_page), pages[0] if pages else None)
        if matched:
            aid = data["aid"]
            # 从原始 pages 里找 cid
            raw_pages = data.get("pages") or []
            cid = next((p["cid"] for p in raw_pages if p["page"] == target_page), None)
            if cid:
                player = requests.get(
                    "https://api.bilibili.com/x/player/v2",
                    params={"aid": aid, "cid": cid},
                    headers=HEADERS, cookies=cookies, timeout=15,
                ).json()
                entries = (player.get("data") or {}).get("subtitle", {}).get("subtitles") or []
                has_en = any((e.get("lan") or "").lower().startswith("en") for e in entries)
                result["has_subtitle"] = has_en
                result["subtitle_count"] = len(entries)
            else:
                result["has_subtitle"] = False
        else:
            result["has_subtitle"] = False

    return result


def build_bilibili_lesson(url: str, cookies_file: str | None = None, download_video: bool = False, whisper_model: str = "large-v3") -> SourceBundle:
    cookies = _load_cookies_txt(cookies_file)
    bvid, part_index = _extract_bvid_and_part(url)

    view = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
    ).json()
    if view.get("code") != 0:
        raise RuntimeError(f"Bilibili view API 失败: {view}")

    data = view["data"]
    aid = data["aid"]
    pages = data.get("pages") or []

    # 定位到指定分 P
    if pages and part_index is not None:
        matched = next((p for p in pages if p["page"] == part_index), None)
        if matched is None:
            raise ValueError(
                f"该视频共 {len(pages)} 集，没有第 p{part_index} 集。"
                f"可用范围：p1 ~ p{len(pages)}"
            )
        cid = matched["cid"]
        episode_title = matched.get("part") or ""
        title = f"{data['title']} - p{part_index}" + (f" {episode_title}" if episode_title else "")
        print(f"  已定位到第 p{part_index} 集：{episode_title or title}")
    elif pages:
        # 有多集但没指定 p，默认第 1 集并提示
        cid = pages[0]["cid"]
        title = data["title"]
        print(
            f"  提示：该视频共 {len(pages)} 集，未指定集数，默认使用 p1。\n"
            f"  如需指定集数，请在链接末尾加 ?p=N，例如：\n"
            f"    python main.py --bilibili \"{url.split('?')[0]}?p=3\""
        )
    else:
        cid = data["cid"]
        title = data["title"]

    subtitle_segments = _fetch_subtitle_segments(aid, cid, cookies)

    cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "bilibili"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 按用户选择下载音频或视频
    if download_video:
        audio_path = _download_video(cache_dir, bvid, cid, cookies)
    else:
        audio_path = _download_audio(cache_dir, bvid, cid, cookies)

    if subtitle_segments:
        return SourceBundle(
            source_type="local_video",
            title=title,
            source_value=url,
            segments=subtitle_segments,
            local_video=audio_path,
        )

    # 无字幕 → 用 Whisper 转录音频（先查缓存 / 已有课程 HTML）
    output_dir = Path(__file__).resolve().parent.parent / "output"
    lesson = build_local_video_lesson(
        str(audio_path),
        whisper_model=whisper_model,
        bvid=bvid,
        output_dir=output_dir,
    )
    lesson.title = title
    lesson.source_value = url
    return lesson


# ── 工具函数 ──────────────────────────────────────────────

def _load_cookies_txt(cookies_file: str | None) -> dict:
    """解析 Netscape 格式的 cookies.txt，返回 {name: value} 字典。"""
    if not cookies_file:
        return {}
    cookies: dict = {}
    try:
        with open(cookies_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    except Exception as exc:
        raise RuntimeError(f"无法读取 cookies 文件 {cookies_file}: {exc}") from exc
    return cookies


def _extract_bvid_and_part(url: str) -> tuple[str, int | None]:
    """从 URL 提取 BV 号和分 P 编号（p=N），没有 p 参数则返回 None。"""
    from urllib.parse import parse_qs, urlparse
    if "b23.tv" in url:
        url = _resolve_bilibili_short_url(url)

    match = re.search(r"/video/(BV[A-Za-z0-9]+)/?", url)
    if match:
        bvid = match.group(1)
    elif url.startswith("BV"):
        bvid = url.split("?")[0]
    else:
        raise ValueError(f"无法从链接中提取 Bilibili BV 号: {url}")

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    p_values = qs.get("p", [])
    part_index = int(p_values[0]) if p_values else None
    return bvid, part_index


def _resolve_bilibili_short_url(url: str) -> str:
    """Expand b23.tv share links before extracting BV id."""
    try:
        with requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True, stream=True) as resp:
            resp.raise_for_status()
            return resp.url
    except Exception as exc:
        raise ValueError(f"无法展开 Bilibili 短链接: {url}") from exc


def _fetch_subtitle_segments(aid: int, cid: int, cookies: dict) -> list[Segment]:
    """拉取字幕，优先取英文；如果同时有中文字幕，填入 segment.translation。"""
    player = requests.get(
        "https://api.bilibili.com/x/player/v2",
        params={"aid": aid, "cid": cid},
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
    ).json()
    if player.get("code") != 0:
        return []

    subtitle = player.get("data", {}).get("subtitle", {}) or {}
    entries = subtitle.get("subtitles", []) or []
    if not entries:
        return []

    # 分别找英文和中文字幕条目
    en_entry = next((e for e in entries if (e.get("lan") or "").lower().startswith("en")), None)
    zh_entry = next((e for e in entries if (e.get("lan") or "").lower().startswith("zh")), None)

    if not en_entry:
        return []

    def fetch_body(entry: dict) -> list[dict]:
        url = entry.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return []
        return requests.get(url, headers=HEADERS, cookies=cookies, timeout=30).json().get("body", []) or []

    en_body = fetch_body(en_entry) if en_entry else []
    zh_body = fetch_body(zh_entry) if zh_entry else []

    segments = []
    for item in en_body:
        text = (item.get("content") or "").strip()
        if not text:
            continue
        segments.append(Segment(
            index=len(segments) + 1,
            text=text,
            start=float(item.get("from", 0)),
            end=float(item.get("to", 0)),
            translation=_match_subtitle_translation(item, zh_body),
        ))

    segments = _merge_subtitle_sentences(segments)

    print("[STEP:subtitle]", flush=True)
    has_zh = any(s.translation for s in segments)
    if has_zh:
        print(f"  已获取双语字幕（英文 {len(segments)} 句 + 中文对照）")
    else:
        print(f"  已获取英文字幕（{len(segments)} 句），中文字幕待翻译")
    return segments


def _merge_subtitle_sentences(segments: list[Segment],
                               gap_threshold: float = 0.8,
                               max_words: int = 40) -> list[Segment]:
    """
    Merge consecutive subtitle entries into complete sentences.

    Priority order:
      1. Text ends with .?!          → definite sentence boundary
      2. Gap to next entry ≥ gap_threshold seconds  → speaker paused, treat as boundary
      3. Accumulated words ≥ max_words              → length safety net
    """
    if not segments:
        return segments

    merged: list[Segment] = []
    buf_text:  str   = ""
    buf_trans: str   = ""
    buf_start: float = 0.0
    buf_end:   float = 0.0

    def flush() -> Segment:
        clean_text  = re.sub(r"\s+", " ", buf_text).strip()
        clean_text  = re.sub(r"\s+'", "'", clean_text)   # fix mid-word splits: "today 's" → "today's"
        clean_trans = re.sub(r"\s+", " ", buf_trans).strip()
        return Segment(index=0, text=clean_text, start=buf_start,
                       end=buf_end, translation=clean_trans)

    for i, seg in enumerate(segments):
        text = seg.text.strip()
        if not text:
            continue

        if not buf_text:
            buf_text  = text
            buf_trans = seg.translation or ""
            buf_start = seg.start or 0.0
            buf_end   = seg.end   or 0.0
        else:
            buf_text  = buf_text.rstrip() + " " + text
            buf_trans = (buf_trans + " " + (seg.translation or "")).strip()
            buf_end   = seg.end or buf_end

        # Rule 1: sentence-final punctuation
        if buf_text.rstrip()[-1] in ".?!":
            merged.append(flush())
            buf_text = buf_trans = ""
            continue

        # Rule 2: gap to next subtitle ≥ threshold → speaker paused here
        if i + 1 < len(segments):
            next_start = segments[i + 1].start or 0.0
            if next_start - buf_end >= gap_threshold:
                merged.append(flush())
                buf_text = buf_trans = ""
                continue

        # Rule 3: length safety net
        if len(buf_text.split()) >= max_words:
            merged.append(flush())
            buf_text = buf_trans = ""

    if buf_text.strip():
        merged.append(flush())

    for i, s in enumerate(merged):
        s.index = i + 1

    print(f"  合并后：{len(merged)} 句（原始字幕 {len(segments)} 条）")
    return merged


def _match_subtitle_translation(en_item: dict, zh_body: list[dict]) -> str:
    if not zh_body:
        return ""

    start = float(en_item.get("from", 0))
    end = float(en_item.get("to", start))
    midpoint = (start + end) / 2

    best_text = ""
    best_overlap = 0.0
    for item in zh_body:
        zh_start = float(item.get("from", 0))
        zh_end = float(item.get("to", zh_start))
        overlap = min(end, zh_end) - max(start, zh_start)
        if zh_start <= midpoint <= zh_end:
            return (item.get("content") or "").strip()
        if overlap > best_overlap:
            best_overlap = overlap
            best_text = (item.get("content") or "").strip()

    return best_text if best_overlap > 0 else ""


def _download_audio(tmp_path: Path, bvid: str, cid: int, cookies: dict) -> Path:
    """下载纯音频流（DASH 格式，保存为 .m4a）。默认路径。"""
    audio_path = tmp_path / f"{bvid}.m4a"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        print("[STEP:download]", flush=True)
        print(f"  复用已下载音频：{audio_path}", flush=True)
        return audio_path

    playurl = requests.get(
        "https://api.bilibili.com/x/player/playurl",
        params={"bvid": bvid, "cid": cid, "qn": 64, "fnval": 16},
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
    ).json()

    if playurl.get("code") != 0:
        _raise_playurl_error(playurl)

    data = playurl.get("data", {}) or {}
    dash = data.get("dash") or {}
    audio_tracks = dash.get("audio") or []

    if audio_tracks:
        # 按 id 降序取最高码率
        audio_tracks.sort(key=lambda x: x.get("id", 0), reverse=True)
        audio_url = audio_tracks[0].get("baseUrl") or audio_tracks[0].get("base_url") or ""
    else:
        # DASH 不可用时降级到 durl（音视频混合流，仍保存为 m4a 占位）
        durls = data.get("durl") or []
        if not durls:
            _raise_playurl_error(playurl)
        audio_url = durls[0]["url"]

    part_path = audio_path.with_suffix(audio_path.suffix + ".part")
    print("[STEP:download]", flush=True)
    print(f"  正在下载音频：{bvid}.m4a …")
    with requests.get(audio_url, headers=HEADERS, cookies=cookies, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        with part_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    part_path.replace(audio_path)
    print(f"  音频已保存：{audio_path}")
    return audio_path


def _download_video(tmp_path: Path, bvid: str, cid: int, cookies: dict) -> Path:
    """下载完整视频流（FLV/MP4 格式）。保留供将来 --download-video 选项使用。"""
    video_path = tmp_path / f"{bvid}.mp4"
    if video_path.exists() and video_path.stat().st_size > 0:
        print("[STEP:download]", flush=True)
        print(f"  复用已下载视频：{video_path}", flush=True)
        return video_path

    playurl = requests.get(
        "https://api.bilibili.com/x/player/playurl",
        params={"bvid": bvid, "cid": cid, "qn": 64, "fnval": 0, "fourk": 1},
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
    ).json()
    if playurl.get("code") != 0:
        _raise_playurl_error(playurl)

    data = playurl.get("data", {}) or {}
    durls = data.get("durl") or []
    if not durls:
        _raise_playurl_error(playurl)

    video_url = durls[0]["url"]
    part_path = video_path.with_suffix(video_path.suffix + ".part")
    print("[STEP:download]", flush=True)
    print(f"  正在下载视频：{bvid}.mp4 …")
    with requests.get(video_url, headers=HEADERS, cookies=cookies, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with part_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    part_path.replace(video_path)
    print(f"  视频已保存：{video_path}")
    return video_path


def _raise_playurl_error(playurl: dict) -> None:
    raise RuntimeError(
        f"Bilibili 音频获取失败（code={playurl.get('code')}）。\n\n"
        "该视频可能需要登录才能下载，请按以下步骤操作：\n"
        "  1. 用浏览器登录 bilibili.com\n"
        "  2. 安装浏览器插件 'Get cookies.txt LOCALLY'（Chrome/Edge 均可）\n"
        "  3. 在 bilibili.com 页面点插件图标，导出 cookies，保存为 cookies.txt\n"
        "  4. 重新运行，加上 --bilibili-cookies 参数：\n"
        "       python main.py --bilibili <url> --bilibili-cookies cookies.txt"
    )
