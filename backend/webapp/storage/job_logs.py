import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
LOGS_DIR = BASE_DIR / "logs"


def ensure_logs_dir() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


def make_job_log_path(job_id: str) -> Path:
    safe_id = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in "-_")[:64]
    if not safe_id:
        safe_id = "unknown"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return ensure_logs_dir() / f"job-{stamp}-{safe_id}.log"


def append_job_log(path: str | Path | None, line: str) -> None:
    if not path:
        return
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(line)
        fh.write("\n")


def tail_log(path: str | Path | None, max_lines: int = 200) -> list[str]:
    if not path:
        return []
    log_path = Path(path)
    if not log_path.is_file():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max(1, int(max_lines)):]


def resolve_log_file(filename: str) -> Path | None:
    safe_name = Path(filename or "").name
    if not safe_name or safe_name != filename or not safe_name.endswith(".log"):
        return None
    candidate = (ensure_logs_dir() / safe_name).resolve()
    logs_root = ensure_logs_dir().resolve()
    if logs_root not in candidate.parents or not candidate.is_file():
        return None
    return candidate
