import os
import time
import shutil
import subprocess
import sys
import threading
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
    ready_dir: Path
    archive_dir: Path
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
    duration = int(os.getenv("DURATION_SECONDS", "4"))
    width = int(os.getenv("WIDTH", "1080"))
    height = int(os.getenv("HEIGHT", "1920"))
    fps = int(os.getenv("FPS", "30"))
    bot_token = os.getenv("BOT_TOKEN")
    owner_chat_id_env = os.getenv("OWNER_CHAT_ID")
    try:
        owner_chat_id = int(owner_chat_id_env) if owner_chat_id_env else None
    except ValueError:
        owner_chat_id = None

    for d in (input_dir, ready_dir, archive_dir):
        d.mkdir(parents=True, exist_ok=True)

    return Settings(
        input_dir=input_dir,
        ready_dir=ready_dir,
        archive_dir=archive_dir,
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

def safe_stem(path: Path) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{path.stem}_{ts}"

def build_ffmpeg_cmd(img_path: Path, out_path: Path, duration: int, width: int, height: int, fps: int):
    w, h = width, height
    vf = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:1[bg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    return [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(img_path),
        "-t", str(duration),
        "-r", str(fps),
        "-filter_complex", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]

def convert_image_to_video(img_path: Path, ready_dir: Path, duration: int, width: int, height: int, fps: int) -> Path:
    out_name = safe_stem(img_path) + ".mp4"
    out_path = ready_dir / out_name
    cmd = build_ffmpeg_cmd(img_path, out_path, duration, width, height, fps)
    log("FFmpeg: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_path

def move_to_archive(img_path: Path, archive_dir: Path) -> Path:
    target = archive_dir / (safe_stem(img_path) + img_path.suffix.lower())
    shutil.move(str(img_path), str(target))
    return target

# -------------------- polling watcher (без watchdog) --------------------

class Poller:
    """
    Простой опрос папки:
    - Каждые POLL_INTERVAL секунд сканим INPUT_DIR
    - Ждём стабилизации размера файла (STABLE_TICKS подряд)
    - После этого конвертируем и переносим в архив
    """
    POLL_INTERVAL = 0.5
    STABLE_TICKS = 3

    def __init__(self, settings: Settings):
        self.settings = settings
        self._seen: Dict[Path, Dict[str, int]] = {}  # path -> {"size": int, "stable": int}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        log("Polling watcher запущен.")
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                log(f"Watcher error: {e}")
            time.sleep(self.POLL_INTERVAL)

    def _scan_once(self):
        # учтём только файлы нужных расширений в корне input_dir
        for p in self.settings.input_dir.glob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                self._tick(p)

        # чистим трекер для файлов, которых больше нет
        tracked = list(self._seen.keys())
        for p in tracked:
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

        # когда файл стабилен N тиков — обрабатываем
        if rec["stable"] >= self.STABLE_TICKS:
            # чтобы не обработать дважды
            self._seen.pop(path, None)
            threading.Thread(target=process_file_sync, args=(path, self.settings), daemon=True).start()

def process_file_sync(path: Path, settings: Settings):
    try:
        log(f"Найден файл: {path.name}")
        video_path = convert_image_to_video(
            path, settings.ready_dir, settings.duration, settings.width, settings.height, settings.fps
        )
        move_to_archive(path, settings.archive_dir)
        log(f"Готово: {video_path.name}")
    except subprocess.CalledProcessError as e:
        log(f"FFmpeg ошибка: {e}")
    except Exception as e:
        log(f"Ошибка обработки {path.name}: {e}")

# -------------------- Telegram bot --------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь мне фото — верну 4‑секундное вертикальное видео 1080×1920.")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

def _best_photo_file(photos):
    return photos[-1] if photos else None

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = context.application.bot_data.get("settings")
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

        tmp_dir = settings.input_dir / "_tg_tmp"
        tmp_dir.mkdir(exist_ok=True)
        tmp_img = tmp_dir / f"tg_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{suffix}"
        await file_obj.download_to_drive(str(tmp_img))

        video_path = convert_image_to_video(
            tmp_img, settings.ready_dir, settings.duration, settings.width, settings.height, settings.fps
        )
        move_to_archive(tmp_img, settings.archive_dir)

        await message.reply_video(video=open(video_path, "rb"), supports_streaming=True, caption="Готово ✅")

    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"FFmpeg ошибка: {e}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# -------------------- main --------------------

def main():
    check_ffmpeg()
    settings = load_settings()

    log(f"INPUT_DIR={settings.input_dir}")
    log(f"READY_DIR={settings.ready_dir}")
    log(f"ARCHIVE_DIR={settings.archive_dir}")

    # запускаем poller
    poller = Poller(settings)
    poller.start()

    # телеграм-бот (опционально)
    if settings.bot_token:
        app = Application.builder().token(settings.bot_token).build()
        app.bot_data["settings"] = settings
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("ping", cmd_ping))
        photo_filter = filters.PHOTO | filters.Document.IMAGE
        app.add_handler(MessageHandler(photo_filter, handle_photo))
        log("Telegram‑бот запущен (polling). Нажми Ctrl+C для выхода.")
        try:
            app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)
        finally:
            poller.stop()
    else:
        log("BOT_TOKEN не задан — бот не поднимаем. Watcher работает. Нажми Ctrl+C для выхода.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            poller.stop()

if __name__ == "__main__":
    main()
