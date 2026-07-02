"""Транскод/ремукс не-нативных форматов при стриминге (последний пункт roadmap).

Не-нативный контейнер/кодек (AVI/XviD, WMV, FLV, MPEG-PS…) браузер получателя
шар-ссылки не играет вовсе, а QMediaPlayer — не на всех бэкендах. Вместо
скачивания целиком: ffmpeg на лету пересобирает поток в **fragmented MP4**
(играется и браузером, и QMediaPlayer), читая исходник по HTTP с нашего же
стрим-сервера (Range уже работает — ffmpeg сам сикает по индексу контейнера).

Стратегию решает ffprobe по фактическим кодекам, а не по расширению:
- h264/hevc/av1 видео и aac/mp3 аудио → ``-c copy`` (дешёвый ремукс, без CPU);
- всё остальное → libx264 (veryfast) / aac.

v1 сознательно без перемотки: выход — chunked-поток неизвестной длины
(``Connection: close``), Range не поддерживается. Для «посмотреть AVI по
ссылке» этого достаточно; перемотка — через нативный путь.

Чистое планирование (``plan_from_probe``/``build_ffmpeg_args``) отделено от
subprocess-обвязки (``probe_media``) — тестируется без ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Кодеки, которые валидны в MP4 и играются браузерами — их не перекодируем.
MP4_COPY_VIDEO_CODECS = {"h264", "hevc", "av1"}
MP4_COPY_AUDIO_CODECS = {"aac", "mp3"}

_PROBE_TIMEOUT_SEC = 25.0


@dataclass(frozen=True)
class TranscodePlan:
    """Что делать с потоками исходника при упаковке в fragmented MP4."""

    video_codec: str | None  # None — видеопотока нет
    audio_codec: str | None  # None — аудиопотока нет
    copy_video: bool
    copy_audio: bool

    @property
    def is_remux_only(self) -> bool:
        """Только пересборка контейнера, без перекодирования (нет нагрузки CPU)."""
        return (self.video_codec is None or self.copy_video) and (
            self.audio_codec is None or self.copy_audio
        )


def transcode_available() -> bool:
    """Есть ли ffmpeg+ffprobe в PATH (оба нужны конвейеру)."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def plan_from_probe(probe: dict) -> TranscodePlan:
    """Построить план по JSON-выводу ffprobe (``-show_streams``).

    Берём первый видео- и первый аудиопоток (типичный кейс медиафайла);
    неизвестный кодек = перекодировать (безопасный дефолт).
    """
    streams = probe.get("streams") or []
    video = next((s for s in streams if str(s.get("codec_type")) == "video"), None)
    audio = next((s for s in streams if str(s.get("codec_type")) == "audio"), None)
    video_codec = str(video.get("codec_name") or "") if video else None
    audio_codec = str(audio.get("codec_name") or "") if audio else None
    return TranscodePlan(
        video_codec=video_codec or None,
        audio_codec=audio_codec or None,
        copy_video=bool(video_codec and video_codec in MP4_COPY_VIDEO_CODECS),
        copy_audio=bool(audio_codec and audio_codec in MP4_COPY_AUDIO_CODECS),
    )


def build_ffmpeg_args(input_url: str, plan: TranscodePlan) -> list[str]:
    """Аргументы ffmpeg: вход по URL, выход — fragmented MP4 в stdout.

    ``frag_keyframe+empty_moov`` — moov сразу, дальше фрагменты: поток можно
    играть с первого байта, не дожидаясь конца (обычный MP4 пишет moov в конце
    и для пайпа непригоден).
    """
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_url,
    ]
    if plan.video_codec is not None:
        if plan.copy_video:
            args += ["-c:v", "copy"]
        else:
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if plan.audio_codec is not None:
        if plan.copy_audio:
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", "192k"]
    args += [
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-",
    ]
    return args


def probe_media(input_url: str) -> dict | None:
    """ffprobe по URL → распарсенный JSON или None (файл не читается/нет ffprobe).

    Блокирует вызывающий поток (HTTP-обработчик) — это осознанно, модель
    сервера потоковая (ThreadingHTTPServer)."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 — фиксированный бинарь, наш URL
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                input_url,
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe failed for transcode input: %s", exc)
        return None
    if completed.returncode != 0:
        logger.warning(
            "ffprobe returned %d for transcode input: %s",
            completed.returncode,
            (completed.stderr or b"")[:200].decode("utf-8", "replace"),
        )
        return None
    try:
        data = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
