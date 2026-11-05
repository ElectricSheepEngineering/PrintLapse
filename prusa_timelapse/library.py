"""Sessions stored on disk: listing, downloads, and deletion.

Every path is checked against the output folder. Download names are built
here, never taken from the request, so a folder name cannot break a header.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from prusa_timelapse.capture import resolve_session
from prusa_timelapse.ffmpeg_util import is_complete_jpeg


FRAME_FILE_NAME = re.compile(r"^frame_\d{6}\.jpg$")
SESSION_LIST_LIMIT = 100
_EXTRA_FILES = ("latest.jpg", "meta.json")


def summarize_frames(frames_dir: Path) -> dict:
    """Count frame files and how many are a gap-free sequence from frame 1.

    Byte totals come from stat. Completeness is only checked along the
    leading run, which is the sequence ffmpeg can encode.
    """
    empty = {"contiguous": 0, "count": 0, "bytes": 0}
    if not frames_dir.is_dir():
        return empty
    try:
        paths = [
            path for path in frames_dir.iterdir()
            if path.is_file() and FRAME_FILE_NAME.match(path.name)
        ]
    except OSError:
        return empty
    paths.sort(key=lambda path: path.name)
    contiguous = 0
    expect = 1
    total = 0
    count = 0
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        count += 1
        total += size
        expected_name = f"frame_{expect:06d}.jpg"
        if expect and path.name == expected_name and is_complete_jpeg(path):
            contiguous = expect
            expect += 1
        elif expect and path.name >= expected_name:
            expect = 0
    return {"contiguous": contiguous, "count": count, "bytes": total}


def frame_names(session: Path) -> list[str]:
    frames_dir = session / "frames"
    if not frames_dir.is_dir():
        return []
    try:
        names = [
            path.name for path in frames_dir.iterdir()
            if path.is_file() and FRAME_FILE_NAME.match(path.name)
        ]
    except OSError:
        return []
    names.sort()
    return names


def session_detail(session: Path, offset: int, limit: int) -> dict:
    summary = summarize_frames(session / "frames")
    names = frame_names(session)
    page = names[offset:offset + limit]
    files = []
    for name in page:
        path = session / "frames" / name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not is_complete_jpeg(path):
            continue
        files.append({"name": name, "bytes": size})
    video = session / "timelapse.mp4"
    try:
        video_bytes = video.stat().st_size if video.is_file() else 0
    except OSError:
        video_bytes = 0
    others = []
    for name in _EXTRA_FILES:
        path = session / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                others.append({"name": name, "bytes": path.stat().st_size})
        except OSError:
            continue
    return {
        "name": session.name,
        "frames_on_disk": summary["contiguous"],
        "frame_files": summary["count"],
        "frames_bytes": summary["bytes"],
        "has_video": video_bytes > 0,
        "video_bytes": video_bytes,
        "others": others,
        "frames_page": {
            "offset": offset,
            "limit": limit,
            "total": len(names),
            "files": files,
        },
    }


def contained_file(session: Path, relative: str) -> Path:
    if not relative or relative.startswith(("/", "\\")) or "\\" in relative:
        raise FileNotFoundError("That file is not in this session.")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FileNotFoundError("That file is not in this session.")
    path = session.joinpath(*parts).resolve()
    try:
        path.relative_to(session.resolve())
    except ValueError as exc:
        raise FileNotFoundError("That file is not in this session.") from exc
    if not path.is_file():
        raise FileNotFoundError("That file is not in this session.")
    return path


def video_file(session: Path) -> Path:
    path = contained_file(session, "timelapse.mp4")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileNotFoundError("That session has no video yet.") from exc
    if size <= 0:
        raise FileNotFoundError("That session has no video yet.")
    return path


def frame_file(session: Path, name: str) -> Path:
    if not FRAME_FILE_NAME.match(name):
        raise FileNotFoundError("That frame is not in this session.")
    path = contained_file(session, f"frames/{name}")
    if not is_complete_jpeg(path):
        raise FileNotFoundError("That frame is not ready yet.")
    return path


def zip_members(session: Path, kind: str, include_video: bool = True) -> list[tuple[Path, str]]:
    if kind not in {"frames", "archive"}:
        raise ValueError("Download must be frames or archive.")
    members: list[tuple[Path, str]] = []
    for name in frame_names(session):
        path = session / "frames" / name
        if path.is_file() and is_complete_jpeg(path):
            members.append((path, f"frames/{name}"))
    if kind == "archive":
        if include_video:
            video = session / "timelapse.mp4"
            try:
                if video.is_file() and video.stat().st_size > 0:
                    members.append((contained_file(session, "timelapse.mp4"), "timelapse.mp4"))
            except (OSError, FileNotFoundError):
                pass
        for name in _EXTRA_FILES:
            path = session / name
            try:
                if path.is_file() and path.stat().st_size > 0:
                    members.append((contained_file(session, name), name))
            except (OSError, FileNotFoundError):
                continue
    if not members:
        if kind == "frames":
            raise FileNotFoundError("That session has no frames to download.")
        raise FileNotFoundError("That session has no frames or video to download.")
    return members


def write_zip(members: list[tuple[Path, str]], dest) -> None:
    """Write a stored zip. JPEG and MP4 are already compressed."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path, arcname in members:
            archive.write(path, arcname)


def download_filename(session_name: str, suffix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", session_name).strip(" .-")
    base = cleaned or "session"
    return f"{base}-{suffix}" if suffix else base


def delete_stored(root: Path, name: str, target: str) -> str:
    session = resolve_session(root, name)
    root_resolved = root.resolve()
    session_resolved = session.resolve()
    try:
        session_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Unknown session.") from exc
    if session_resolved == root_resolved:
        raise ValueError("Unknown session.")
    if target == "session":
        shutil.rmtree(session_resolved)
        return target
    if target == "frames":
        frames = session / "frames"
        if not frames.exists():
            raise FileNotFoundError("That session has no frames.")
        frames_resolved = frames.resolve()
        try:
            frames_resolved.relative_to(session_resolved)
        except ValueError as exc:
            raise ValueError("Unknown session.") from exc
        if frames.is_symlink():
            frames.unlink()
        else:
            shutil.rmtree(frames_resolved)
        return target
    if target == "video":
        video = video_file(session)
        video.unlink()
        return target
    raise ValueError("Delete target must be session, frames, or video.")


def parse_byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive byte span. None means the whole file.

    A range that starts past the end raises ValueError. The caller answers
    that with HTTP 416.
    """
    if not header:
        return None
    if not header.startswith("bytes="):
        raise ValueError("Range must be in bytes.")
    if "," in header:
        return None
    spec = header[len("bytes="):]
    if "-" not in spec:
        raise ValueError("Range must be in bytes.")
    start_text, end_text = spec.split("-", 1)
    if start_text == "" and end_text == "":
        raise ValueError("Range must be in bytes.")
    try:
        if start_text == "":
            length = int(end_text)
            if length <= 0 or size <= 0:
                raise ValueError("Range is outside the file.")
            start = max(0, size - length)
            return start, size - 1
        start = int(start_text)
        end = size - 1 if end_text == "" else int(end_text)
    except ValueError as exc:
        raise ValueError("Range must be in bytes.") from exc
    if size <= 0 or start < 0 or start >= size or end < start:
        raise ValueError("Range is outside the file.")
    return start, min(end, size - 1)
