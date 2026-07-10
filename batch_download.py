"""
Пакетная загрузка серий из Kodik.

Скачивает диапазон серий [start..end] в выбранной озвучке и качестве:
  * серии — последовательно (понятный прогресс, сеть не забивается);
  * сегменты одной серии — пулом потоков (см. fast_download.SEGMENT_WORKERS);
  * сборка через ffmpeg concat + '-c copy' (без перекодирования);
  * результат — один ZIP на все серии.

Задачи выполняются в фоне; веб-интерфейс опрашивает get_status().

Публичное API:
    start_job(...)   -> job_id (str)
    get_status(id)   -> dict | None   (безопасно отдавать в JSON)
    get_zip_path(id) -> (path, name) | None
    cancel_job(id)   -> None
    cleanup_jobs()   -> None
"""

import os
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import config
from fast_download import (SEGMENT_WORKERS, check_ffmpeg, combine_segments,
                           download_segment, get_segments)
from getters import get_download_link, get_url_data

BATCH_DIR = getattr(config, "BATCH_DIR", "batch_tmp")
JOB_TTL_SECONDS = getattr(config, "BATCH_JOB_TTL", 3 * 60 * 60)  # 3 часа
MAX_EPISODES = getattr(config, "BATCH_MAX_EPISODES", 500)

_jobs = {}
_jobs_lock = threading.Lock()

_FORBIDDEN = {
    "\\": "-", "/": "-", ":": "-", "*": "-", "?": "", '"': "'",
    "<": "[", ">": "]", "|": "-", "\n": " ", "\r": " ", "\t": " ",
    "«": "'", "»": "'", "„": "'", "“": "'",
}


def _sanitize(name: str, maxlen: int = 100) -> str:
    name = (name or "").strip()
    for bad, good in _FORBIDDEN.items():
        name = name.replace(bad, good)
    while "  " in name:
        name = name.replace("  ", " ")
    name = name.strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    return name or "video"


def _download_one(serv, kid, seria, translation_id, quality, out_dir,
                  filename, metadata, stop: threading.Event):
    """Скачивает одну серию и собирает её в out_dir/<filename>.mp4."""
    id_type = "shikimori" if serv == "sh" else "kinopoisk"
    link = get_download_link(kid, id_type, seria, translation_id, config.KODIK_TOKEN)
    base = "https:" + link

    manifest = get_url_data(base + quality + ".mp4:hls:manifest.m3u8")
    segments = get_segments(manifest, base)
    if not segments:
        raise FileNotFoundError("нет сегментов — попробуйте другое качество")

    seg_dir = os.path.join(out_dir, "_seg_%s" % seria)
    if os.path.exists(seg_dir):
        shutil.rmtree(seg_dir, ignore_errors=True)
    os.makedirs(seg_dir, exist_ok=True)

    def _pull(seg):
        if stop.is_set():
            return
        download_segment(seg[0], os.path.join(seg_dir, seg[1] + ".ts"))

    try:
        with ThreadPoolExecutor(max_workers=SEGMENT_WORKERS) as pool:
            list(pool.map(_pull, segments))

        if stop.is_set():
            return None

        combine_segments(seg_dir, name=filename, metadata=metadata)
        built = os.path.join(seg_dir, filename + ".mp4")
        final = os.path.join(out_dir, filename + ".mp4")
        shutil.move(built, final)
        return final
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)


def _set(job_id, **kw):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw)


def _run(job_id):
    with _jobs_lock:
        job = _jobs[job_id]
    p = job["_params"]
    stop = job["_stop"]

    serv, kid = p["serv"], p["id"]
    translation_id, translation_name = p["translation_id"], p["translation_name"]
    quality = p["quality"]
    start, end = p["start"], p["end"]
    title = p["title"]

    out_dir = os.path.join(BATCH_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)

    width = len(str(end))
    safe_title = _sanitize(title, 70) if title else ""

    _set(job_id, status="running", phase="downloading")

    for seria in range(start, end + 1):
        if stop.is_set():
            _set(job_id, status="cancelled", phase="cancelled")
            shutil.rmtree(out_dir, ignore_errors=True)
            return

        _set(job_id, current=seria)
        with _jobs_lock:
            _jobs[job_id]["episodes"][seria]["status"] = "downloading"

        num = str(seria).zfill(width)
        parts = ([safe_title] if safe_title else []) + ["Серия-%s" % num, "%sp" % quality]
        fname = _sanitize("-".join(parts), 120)

        metadata = {
            "title": ("%s - Серия %s" % (safe_title, seria)) if safe_title else ("Серия %s" % seria),
            "track": seria,
            "artist": translation_name,
        }

        try:
            path = _download_one(serv, kid, seria, translation_id, quality,
                                 out_dir, fname, metadata, stop)
            if path is None:  # отменено во время скачивания
                _set(job_id, status="cancelled", phase="cancelled")
                shutil.rmtree(out_dir, ignore_errors=True)
                return
            with _jobs_lock:
                _jobs[job_id]["episodes"][seria]["status"] = "done"
                _jobs[job_id]["done"] += 1
        except Exception as ex:
            with _jobs_lock:
                _jobs[job_id]["episodes"][seria]["status"] = "failed"
                _jobs[job_id]["episodes"][seria]["error"] = str(ex)
                _jobs[job_id]["failed"] += 1

    if stop.is_set():
        _set(job_id, status="cancelled", phase="cancelled")
        shutil.rmtree(out_dir, ignore_errors=True)
        return

    mp4s = [f for f in os.listdir(out_dir) if f.endswith(".mp4")]
    if not mp4s:
        _set(job_id, status="error", phase="error", finished_at=time.time(),
             error="Ни одну серию не удалось скачать. Попробуйте другое качество или озвучку.")
        shutil.rmtree(out_dir, ignore_errors=True)
        return

    _set(job_id, phase="zipping")
    zparts = ([safe_title] if safe_title else []) + [
        "Серии-%s-%s" % (start, end), _sanitize(translation_name, 40), "%sp" % quality]
    zip_name = _sanitize("_".join(zparts), 150) + ".zip"
    zip_path = os.path.join(out_dir, zip_name)

    try:
        # ZIP_STORED: mp4 уже сжаты, компрессия только жгла бы CPU
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for f in sorted(mp4s):
                zf.write(os.path.join(out_dir, f), arcname=f)
    except Exception as ex:
        _set(job_id, status="error", phase="error", finished_at=time.time(),
             error="Не удалось собрать zip: %s" % ex)
        return

    # Отдельные серии уже внутри архива — удаляем их, чтобы не занимали место вдвойне
    for f in mp4s:
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass

    _set(job_id, status="done", phase="done", zip_path=zip_path,
         zip_name=zip_name, finished_at=time.time())


# --- Публичное API ------------------------------------------------------------
def start_job(serv, id, translation_id, translation_name, quality, start, end, title=""):
    if serv not in ("sh", "kp"):
        raise ValueError("Неизвестный источник: %s" % serv)

    start, end = int(start), int(end)
    if start < 1:
        start = 1
    if end < start:
        raise ValueError("Конечная серия меньше начальной")
    if end - start + 1 > MAX_EPISODES:
        raise ValueError("Слишком большой диапазон (максимум %s серий за раз)" % MAX_EPISODES)

    quality = str(quality).replace("p", "")
    if quality not in ("360", "480", "720"):
        raise ValueError("Недопустимое качество (доступно 360/480/720)")

    check_ffmpeg()  # ModuleNotFoundError -> 500 на уровне роута

    os.makedirs(BATCH_DIR, exist_ok=True)
    cleanup_jobs()

    job_id = uuid.uuid4().hex[:12]
    stop = threading.Event()
    episodes = {n: {"num": n, "status": "pending"} for n in range(start, end + 1)}

    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "status": "queued", "phase": "queued",
            "total": end - start + 1, "done": 0, "failed": 0,
            "current": None, "error": None,
            "zip_path": None, "zip_name": None,
            "episodes": episodes,
            "created_at": time.time(), "finished_at": None,
            "_params": {
                "serv": serv, "id": str(id), "translation_id": str(translation_id),
                "translation_name": translation_name or "Неизвестно",
                "quality": quality, "start": start, "end": end, "title": title or "",
            },
            "_stop": stop,
        }

    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def get_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        eps = [
            {"num": e["num"], "status": e["status"], "error": e.get("error")}
            for e in sorted(job["episodes"].values(), key=lambda x: x["num"])
        ]
        return {
            "id": job["id"], "status": job["status"], "phase": job["phase"],
            "total": job["total"], "done": job["done"], "failed": job["failed"],
            "current": job["current"], "error": job["error"],
            "zip_name": job["zip_name"], "ready": job["status"] == "done",
            "episodes": eps,
        }


def get_zip_path(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "done" and job["zip_path"]:
            return job["zip_path"], job["zip_name"]
    return None


def cancel_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job["_stop"].set()


def cleanup_jobs():
    """Удаляет протухшие завершённые задачи и осиротевшие директории."""
    now = time.time()
    to_del = []
    with _jobs_lock:
        for jid, job in _jobs.items():
            fin = job.get("finished_at")
            if fin and now - fin > JOB_TTL_SECONDS:
                to_del.append(jid)
        for jid in to_del:
            _jobs.pop(jid, None)
    for jid in to_del:
        shutil.rmtree(os.path.join(BATCH_DIR, jid), ignore_errors=True)

    if os.path.isdir(BATCH_DIR):
        with _jobs_lock:
            known = set(_jobs.keys())
        for name in os.listdir(BATCH_DIR):
            p = os.path.join(BATCH_DIR, name)
            if os.path.isdir(p) and name not in known:
                try:
                    if now - os.path.getmtime(p) > JOB_TTL_SECONDS:
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass


# Как часто фоновый уборщик проверяет просроченные задачи (в секундах).
# По умолчанию — каждые 10 минут, но не реже, чем раз в четверть TTL.
CLEANUP_INTERVAL = getattr(config, "BATCH_CLEANUP_INTERVAL",
                           max(60, min(600, JOB_TTL_SECONDS // 4)))


def _reaper():
    """Фоновый поток: периодически чистит просроченные задачи и файлы,
    чтобы очистка не зависела от запуска новых загрузок."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            cleanup_jobs()
        except Exception:
            pass


def start_cleanup_thread():
    """Запускает фоновый уборщик один раз. Вызывается при импорте модуля."""
    t = threading.Thread(target=_reaper, daemon=True)
    t.start()


# Стартуем уборщик при импорте модуля (main.py импортирует batch_download)
start_cleanup_thread()
