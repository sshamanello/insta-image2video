import os
import time
import shutil
import subprocess
import sys
import threading
import uuid
import queue
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# -------------------- утилиты/настройки --------------------

def log(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

@dataclass
class Settings:
    input_dir: Path
    work_dir: Path
    ready_dir: Path
    archive_dir: Path
    failed_dir: Path
    tmp_dir: Path            # для ТГ-временок (вне inbox!)
    duration: int
    width: int
    height: int
    fps: int
    bot_token: Optional[str]
    owner_chat_id: Optional[int]

def load_settings() -> Settings:
    load_dotenv()
    input_dir = Path(os.getenv("INPUT_DIR", "./inbox")).resolve()
    ready_dir = Path(os.getenv("READY_DIR", "./readyforinstagram")).resolve()
    archive_dir = Path(os.getenv("ARCHIVE_DIR", "./archive")).resolve()
    work_dir = Path(os.getenv("WORK_DIR", "./work")).resolve()
    failed_dir = Path(os.getenv("FAILED_DIR", "./failed")).resolve()
    tmp_dir = Path(os.getenv("TMP_DIR", "./tmp")).resolve()

    duration = int(os.getenv("DURATION_SECONDS", "12"))  # по умолчанию 12 сек
    width = int(os.getenv("WIDTH", "1080"))
    height = int(os.getenv("HEIGHT", "1920"))
    fps = int(os.getenv("FPS", "25"))
    bot_token = os.getenv("BOT_TOKEN")
    owner_chat_id_env = os.getenv("OWNER_CHAT_ID")
    try:
        owner_chat_id = int(owner_chat_id_env) if owner_chat_id_env else None
    except ValueError:
        owner_chat_id = None

    for d in (input_dir, work_dir, ready_dir, archive_dir, failed_dir, tmp_dir):
        d.mkdir(parents=True, exist_ok=True)

    return Settings(
        input_dir=input_dir,
        work_dir=work_dir,
        ready_dir=ready_dir,
        archive_dir=archive_dir,
        failed_dir=failed_dir,
        tmp_dir=tmp_dir,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        bot_token=bot_token,
        owner_chat_id=owner_chat_id,
    )

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        log("FFmpeg не найден в PATH. Установи ffmpeg и перезапусти.")
        sys.exit(1)

def unique_stem(path_or_name: str) -> str:
    stem = Path(path_or_name).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    uid = uuid.uuid4().hex[:8]
    return f"{stem}_{ts}_{uid}"

def build_ffmpeg_cmd(img_path: Path, out_path: Path, duration: int, width: int, height: int, fps: int):
    # Вертикаль 1080x1920, размытый фон + оригинал по центру
    w, h = width, height
    vf = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=20:1[bg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    return [
        "ffmpeg",
        "-y",
        "-loglevel", "error", "-stats",
        "-loop", "1",
        "-i", str(img_path),
        "-t", str(duration),
        "-r", str(fps),
        "-filter_complex", vf,
        "-shortest",
        "-c:v", "libx264",
        "-b:v", "3M", "-maxrate", "3M", "-bufsize", "6M",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]

def convert_image_to_video(img_path: Path, ready_dir: Path, duration: int, width: int, height: int, fps: int) -> Path:
    out_path = ready_dir / (unique_stem(img_path) + ".mp4")
    cmd = build_ffmpeg_cmd(img_path, out_path, duration, width, height, fps)
    log("FFmpeg: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_path

def safe_move(src: Path, dst_dir: Path, keep_ext: bool = True) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    name = unique_stem(src)
    if keep_ext:
        name += src.suffix.lower()
    dst = dst_dir / name
    os.replace(str(src), str(dst))  # атомарный перенос в пределах диска
    return dst

# -------------------- очередь работ (1 воркер) --------------------

@dataclass
class Job:
    src_path: Path  # путь к файлу в WORK_DIR (мы всегда работаем из work/)
    is_temp: bool   # был ли файл временным (например из ТГ), влияет только на логи

class JobQueue:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.q: "queue.Queue[Job]" = queue.Queue()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self._stop = threading.Event()

    def start(self):
        self.worker.start()

    def stop(self):
        self._stop.set()
        self.q.put(None)  # разблокировать
        self.worker.join(timeout=2)

    def add(self, job: Job):
        self.q.put(job)

    def _worker(self):
        log("Worker: стартовал.")
        while not self._stop.is_set():
            job = self.q.get()
            if job is None:
                break
            src = job.src_path
            try:
                log(f"Worker: обрабатываю {src.name}")
                # Конвертация
                video_path = convert_image_to_video(
                    src, self.settings.ready_dir,
                    self.settings.duration, self.settings.width, self.settings.height, self.settings.fps
                )
                # Успех → исходник в archive
                safe_move(src, self.settings.archive_dir, keep_ext=True)
                log(f"Worker: готово {video_path.name}")
            except subprocess.CalledProcessError as e:
                log(f"FFmpeg ошибка: {e}")
                # Даже при ошибке переносим исходник в failed/, чтобы не зациклиться
                try:
                    safe_move(src, self.settings.failed_dir, keep_ext=True)
                except Exception as e2:
                    log(f"Не удалось перенести в failed/: {e2}")
            except Exception as e:
                log(f"Ошибка обработки {src.name}: {e}")
                try:
                    safe_move(src, self.settings.failed_dir, keep_ext=True)
                except Exception as e2:
                    log(f"Не удалось перенести в failed/: {e2}")
            finally:
                self.q.task_done()

# -------------------- polling watcher (без watchdog) --------------------

class Poller:
    """
    Простой опрос папки:
    - Каждые POLL_INTERVAL секунд сканим INPUT_DIR
    - Ждём стабилизации размера файла (STABLE_TICKS подряд)
    - Как только файл стабилен — ПЕРЕНОСИМ его в WORK_DIR (claim) и ставим в очередь
    """
    POLL_INTERVAL = 0.5
    STABLE_TICKS = 3

    def __init__(self, settings: Settings, jobs: JobQueue):
        self.settings = settings
        self.jobs = jobs
        self._seen: Dict[Path, Dict[str, int]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        log("Polling watcher: запущен.")
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                log(f"Watcher error: {e}")
            time.sleep(self.POLL_INTERVAL)

    def _scan_once(self):
        for p in self.settings.input_dir.glob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                self._tick(p)

        # чистим трекер для исчезнувших файлов
        for p in list(self._seen.keys()):
            if not p.exists():
                self._seen.pop(p, None)

    def _tick(self, path: Path):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return

        rec = self._seen.get(path)
        if rec is None:
            self._seen[path] = {"size": size, "stable": 0}
            return

        if size == rec["size"] and size > 0:
            rec["stable"] += 1
        else:
            rec["size"] = size
            rec["stable"] = 0

        if rec["stable"] >= self.STABLE_TICKS:
            # Claim: переносим в WORK и ставим в очередь
            self._seen.pop(path, None)
            try:
                claimed = safe_move(path, self.settings.work_dir, keep_ext=True)
                self.jobs.add(Job(src_path=claimed, is_temp=False))
                log(f"Claimed: {claimed.name}")
            except Exception as e:
                log(f"Не удалось перенести в work/: {e}")

# -------------------- Telegram bot --------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь мне фото — верну вертикальное видео. Длительность по умолчанию 12 сек. Настройка в .env (DURATION_SECONDS).")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

def _best_photo_file(photos):
    return photos[-1] if photos else None

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings: Settings = context.application.bot_data["settings"]
    jobs: JobQueue = context.application.bot_data["jobs"]
    try:
        message = update.message
        file_obj = None
        suffix = ".jpg"

        if message.photo:
            photo = _best_photo_file(message.photo)
            file_obj = await photo.get_file()
            suffix = ".jpg"
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
            file_obj = await message.document.get_file()
            name = message.document.file_name or ""
            suffix = Path(name).suffix or ".jpg"
        else:
            return

        # Скачиваем во временный файл вне inbox
        tmp_name = f"tg_{unique_stem('image')}{suffix}"
        tmp_path = settings.tmp_dir / tmp_name
        await file_obj.download_to_drive(str(tmp_path))

        # Сразу переносим в WORK и ставим в очередь (единый конвейер)
        claimed = safe_move(tmp_path, settings.work_dir, keep_ext=True)
        jobs.add(Job(src_path=claimed, is_temp=True))
        await message.reply_text("Принял, конвертирую…")

        # Когда воркер закончит — пришлём видео
        # Простой поллинг готового файла (имя видео заранее не знаем). Отправим самое свежее из ready_dir.
        # Это простая стратегия, в продакшене можно делать корелляцию по id.
        def find_latest_mp4(dirpath: Path) -> Optional[Path]:
            mp4s = list(dirpath.glob("*.mp4"))
            if not mp4s:
                return None
            return max(mp4s, key=lambda p: p.stat().st_mtime)

        # подождём до 60 сек результат (обычно быстрее)
        for _ in range(120):
            latest = find_latest_mp4(settings.ready_dir)
            if latest and datetime.now().timestamp() - latest.stat().st_mtime <= 70:
                # отправим файл
                await message.reply_video(video=open(latest, "rb"), supports_streaming=True, caption="Готово ✅")
                break
            await asyncio.sleep(0.5)

    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"FFmpeg ошибка: {e}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# -------------------- main --------------------

import asyncio

def main():
    check_ffmpeg()
    settings = load_settings()

    log(f"INPUT_DIR={settings.input_dir}")
    log(f"WORK_DIR={settings.work_dir}")
    log(f"READY_DIR={settings.ready_dir}")
    log(f"ARCHIVE_DIR={settings.archive_dir}")
    log(f"FAILED_DIR={settings.failed_dir}")

    # очередь + один воркер
    jobs = JobQueue(settings)
    jobs.start()

    # поллер inbox
    poller = Poller(settings, jobs)
    poller.start()

    # телеграм-бот (опционально)
    if settings.bot_token:
        app = Application.builder().token(settings.bot_token).build()
        app.bot_data["settings"] = settings
        app.bot_data["jobs"] = jobs
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("ping", cmd_ping))
        photo_filter = filters.PHOTO | filters.Document.IMAGE
        app.add_handler(MessageHandler(photo_filter, handle_photo))
        log("Telegram‑бот запущен (polling). Нажми Ctrl+C для выхода.")
        try:
            app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)
        finally:
            poller.stop()
            jobs.stop()
    else:
        log("BOT_TOKEN не задан — бот не поднимаем. Watcher работает. Нажми Ctrl+C для выхода.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            poller.stop()
            jobs.stop()

if __name__ == "__main__":
    main()
