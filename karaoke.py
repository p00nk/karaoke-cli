#!/usr/bin/env python3
"""
karaoke-cli — конвертирует музыкальное видео или аудио в версию с субтитрами-каракое.

Режимы входных данных:
  URL  — ссылка на YouTube/Rutube/etc. (скачивается через yt-dlp)
  Файл — локальный аудиофайл (.mp3, .flac, .wav, ...) → видео со статичным фоном

Источники текстов (приоритет):
  1. YouTube субтитры (json3, word-level тайминг)
  2. lrclib.net + WhisperX forced alignment
  3. WhisperX транскрипция

Использование:
  python karaoke.py <url|файл> [-o <dir>] [--lang ru] [--quality low] [--max-height 480]
  python karaoke.py song.mp3 --bg ./karaoke_bg.png --lang ru
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests

VERSION = "1.0.0"


def log(msg: str):
    print(f"\033[36m[karaoke]\033[0m {msg}", flush=True)

def err(msg: str):
    print(f"\033[31m[ERROR]\033[0m {msg}", file=sys.stderr, flush=True)


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "ftp://", "rtmp://"))


# ─── 1a. Скачивание видео + субтитры (URL) ───────────────────────────────────

def download_video(url: str, work_dir: Path,
                   lang: Optional[str] = None,
                   download_subs: bool = False) -> tuple[Path, dict]:
    import yt_dlp

    langs = [lang] if lang else ["en", "ru"]
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(work_dir / "video.%(ext)s"),
        "noplaylist": True,
        "retries": 2,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "quiet": True,
        "no_warnings": True,
    }
    if download_subs:
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": langs,
            "subtitlesformat": "json3",
            "ignoreerrors": "only_download",
        })

    log(f"Скачиваю: {url}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # При плейлисте info содержит вложенный 'entries' — берём первый элемент
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else None
    if not info:
        raise RuntimeError("yt-dlp не вернул информацию о видео")

    with yt_dlp.YoutubeDL(opts) as ydl:
        filename = ydl.prepare_filename(info)

    video_path = Path(filename)
    if not video_path.exists():
        candidates = [p for p in sorted(work_dir.glob("video.*"))
                      if p.suffix not in (".json3", ".vtt", ".srt", ".ass")]
        if not candidates:
            raise FileNotFoundError("yt-dlp не создал видеофайл")
        video_path = candidates[0]

    log(f"Скачано: {video_path.name}")
    return video_path, info


def fetch_youtube_subtitles(work_dir: Path,
                            lang: Optional[str] = None) -> Optional[list[dict]]:
    """
    Парсит скачанные YouTube субтитры (json3) с word-level тайм-кодами.
    Если lang указан — использует только субтитры на этом языке.
    Возвращает [{word, start, end}] или None.
    """
    subs_files = sorted(work_dir.glob("video.*.json3"))
    if not subs_files:
        return None

    def _file_lang(f: Path) -> str:
        """video.ru-orig.json3 → 'ru', video.en.json3 → 'en'"""
        part = f.stem.split(".", 1)[1] if "." in f.stem else ""
        return part.split("-")[0]

    if lang:
        # Используем только файлы на запрошенном языке
        matched = [f for f in subs_files if _file_lang(f) == lang]
        if not matched:
            log(f"YouTube субтитры на языке '{lang}' не найдены — перехожу к lrclib.")
            return None
        orig = [f for f in matched if "-orig" in f.stem]
        subs_path = orig[0] if orig else matched[0]
    else:
        # Язык не задан: предпочитаем оригинал
        orig = [f for f in subs_files if "-orig" in f.stem]
        subs_path = orig[0] if orig else subs_files[0]

    lang_tag = subs_path.stem.split(".", 1)[1] if "." in subs_path.stem else "?"
    log(f"Найдены YouTube субтитры: {subs_path.name} (язык: {lang_tag})")

    try:
        data = json.loads(subs_path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Ошибка чтения субтитров: {e}")
        return None

    # Проверяем наличие word-level тайм-кодов (tOffsetMs).
    # Без них субтитры содержат только текст целыми блоками — для каракое бесполезны.
    has_word_timing = any(
        s.get("tOffsetMs") is not None
        for ev in data.get("events", [])
        for s in ev.get("segs", [])
        if s.get("utf8", "").strip()
    )
    if not has_word_timing:
        log("YouTube субтитры найдены, но нет word-level тайм-кодов — перейду к lrclib.")
        return None

    words = []
    for event in data.get("events", []):
        t_start_ms = event.get("tStartMs", 0)
        d_dur_ms = event.get("dDurMs", 500)
        all_segs = event.get("segs", [])

        # Собираем слова события; \n в тексте = граница строки → _line_end на предыдущем слове
        event_words: list[dict] = []
        for i, seg in enumerate(all_segs):
            raw = seg.get("utf8", "")
            if raw == "\n":
                if event_words:
                    event_words[-1]["_line_end"] = True
                continue
            text = raw.strip()
            if not text:
                continue
            t_off = seg.get("tOffsetMs", 0)
            w_start = (t_start_ms + t_off) / 1000.0
            # Конец слова — начало следующего или конец события
            next_word_segs = [s for s in all_segs[i + 1:] if s.get("utf8", "").strip() and s.get("utf8") != "\n"]
            if next_word_segs and next_word_segs[0].get("tOffsetMs") is not None:
                w_end = (t_start_ms + next_word_segs[0]["tOffsetMs"]) / 1000.0
            else:
                w_end = (t_start_ms + d_dur_ms) / 1000.0
            event_words.append({
                "word": text,
                "start": w_start,
                "end": max(w_end, w_start + 0.05),
            })

        if event_words:
            event_words[-1]["_line_end"] = True   # конец события = граница строки
            words.extend(event_words)

    if not words:
        log("Субтитры найдены, но не содержат word-level тайм-кодов.")
        return None

    log(f"YouTube субтитры: {len(words)} слов с тайм-кодами.")
    return words


def parse_artist_title(info: dict) -> tuple[str, str]:
    title = info.get("title", "unknown")
    artist = info.get("artist") or info.get("uploader") or info.get("channel") or ""
    if " - " in title and not info.get("artist"):
        a, t = title.split(" - ", 1)
        artist, title = a.strip(), t.strip()
    return artist.strip(), title.strip()


# ─── 1b. Загрузка локального аудиофайла ──────────────────────────────────────

def _fix_cp1251_mojibake(s: str) -> str:
    """
    Исправляет типичный mojibake в русских MP3-тегах:
    байты CP1251 были ошибочно декодированы как Latin-1.
    Например: 'Àëèñà' → 'Алиса'
    """
    if not s:
        return s
    try:
        fixed = s.encode("latin-1").decode("cp1251")
        if any("Ѐ" <= c <= "ӿ" for c in fixed):
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return s


def load_local_audio(audio_path: Path, work_dir: Path) -> tuple[Path, dict]:
    """
    Копирует локальный аудиофайл, извлекает метаданные.
    Приоритет: mutagen (корректно читает кодировки) → ffprobe + фикс CP1251.
    """
    import shutil

    dest = work_dir / ("audio" + audio_path.suffix)
    shutil.copy2(audio_path, dest)
    log(f"Аудиофайл: {audio_path.name}")

    title = artist = ""

    # Попытка 1: mutagen
    try:
        import mutagen
        mf = mutagen.File(str(dest), easy=True)
        if mf:
            title  = str(mf.get("title",  [""])[0])
            artist = str(mf.get("artist", [""])[0])
            # Применяем фикс в любом случае — mutagen тоже может вернуть mojibake
            title  = _fix_cp1251_mojibake(title)
            artist = _fix_cp1251_mojibake(artist)
            log(f"Теги (mutagen): artist={artist!r} title={title!r}")
    except Exception:
        pass

    # Попытка 2: ffprobe + исправление CP1251
    if not title and not artist:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(dest)],
            capture_output=True, text=True,
        )
        fmt  = json.loads(result.stdout).get("format", {})
        tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
        title  = _fix_cp1251_mojibake(tags.get("title", ""))
        artist = _fix_cp1251_mojibake(
            tags.get("artist") or tags.get("album_artist")
            or tags.get("albumartist") or ""
        )
        log(f"Теги (ffprobe+fix): artist={artist!r} title={title!r}")

    title  = title  or audio_path.stem
    artist = artist or ""

    # Если тегов нет — пытаемся разобрать имя файла как "Artist - Title"
    if not artist and " - " in audio_path.stem:
        parts = audio_path.stem.split(" - ", 1)
        artist, title = parts[0].strip(), parts[1].strip()

    info = {"title": title, "artist": artist, "uploader": artist}
    log(f"Метаданные: «{artist}» — «{title}»")
    return dest, info


# ─── 2. Отделение вокала ─────────────────────────────────────────────────────

def separate_vocals(video_path: Path, work_dir: Path) -> tuple[Path, Path]:
    """Возвращает (no_vocals, vocals) — оба нужны: no_vocals для аудио, vocals для alignment."""
    log("Запускаю Demucs для отделения вокала...")
    demucs_out = work_dir / "demucs"
    subprocess.run(
        [sys.executable, "-m", "demucs", "--two-stems=vocals",
         "--out", str(demucs_out), str(video_path)],
        check=True,
    )
    no_vocals_list = list(demucs_out.rglob("no_vocals.wav"))
    vocals_list    = list(demucs_out.rglob("vocals.wav"))
    if not no_vocals_list:
        raise FileNotFoundError("Demucs не создал no_vocals.wav")
    no_vocals = no_vocals_list[0]
    vocals    = vocals_list[0] if vocals_list else no_vocals  # fallback на случай ошибки
    log(f"Инструментал: {no_vocals}")
    log(f"Вокал:        {vocals}")
    return no_vocals, vocals


# ─── 3. Тексты + синхронизация ───────────────────────────────────────────────

def _parse_lrc(lrc_text: str) -> list[dict]:
    lines = []
    for m in re.finditer(r"\[(\d+):(\d+\.\d+)\](.*)", lrc_text):
        mins, secs, text = m.groups()
        t = int(mins) * 60 + float(secs)
        text = text.strip()
        if text:
            lines.append({"time": t, "text": text})
    return sorted(lines, key=lambda x: x["time"])


def _simplify_title(title: str) -> str:
    """Убирает продюсерские теги, ремикс-пометки и год из названия трека.
    'Спасибо (БИТОДЕЛЬНЯ prod.) 2019' → 'Спасибо'
    'Все решено (Remastered 2021)' → 'Все решено'
    """
    simplified = re.sub(r'\s*[\(\[\{][^\)\]\}]*[\)\]\}]', '', title).strip()
    simplified = re.sub(r'\s+\d{4}\s*$', '', simplified).strip()
    return simplified or title


def fetch_lrclib(artist: str, title: str,
                  duration: float = 0.0) -> Optional[tuple[list[dict], float]]:
    """
    Ищет синхронизированные тексты на lrclib.net.
    Возвращает (lines, lrc_duration) — длительность нужна для масштабирования тайминга.
    Если передана длительность трека, выбирает версию с наиболее близкой длительностью
    (избегает выбора remix/live/другой версии).
    При отсутствии результатов повторяет поиск с упрощённым названием (без скобок/года).
    """
    log(f"Ищу тексты на lrclib.net: «{artist}» — «{title}»")
    try:
        def _search(a: str, t: str) -> list:
            r = requests.get(
                "https://lrclib.net/api/search",
                params={"artist_name": a, "track_name": t},
                timeout=15,
            )
            r.raise_for_status()
            return [x for x in r.json() if x.get("syncedLyrics")]

        synced = _search(artist, title)

        # Fallback: упрощённое название (без скобок, без года)
        if not synced:
            simple = _simplify_title(title)
            if simple != title:
                log(f"Не найдено, повторяю с упрощённым названием: «{simple}»")
                synced = _search(artist, simple)

        if not synced:
            log("Синхронизированных текстов в lrclib.net не найдено.")
            return None

        # Если есть длительность — выбираем ближайшую версию
        if duration > 0:
            best = min(synced, key=lambda r: abs(r.get("duration", 0) - duration))
            diff = abs(best.get("duration", 0) - duration)
            log(f"Тексты найдены (длит. {best.get('duration', '?')}с, "
                f"отличие от трека {diff:.1f}с): {best.get('trackName', '')}")
        else:
            best = synced[0]
            log(f"Тексты найдены: {best.get('trackName', '')}")

        lrc_duration = float(best.get("duration") or 0.0)
        return _parse_lrc(best["syncedLyrics"]), lrc_duration

    except Exception as e:
        log(f"lrclib.net недоступен: {e}")
    return None


def _detect_lang_from_lrc(lrc_lines: list[dict]) -> str:
    """Авто-определение языка по тексту LRC: ru если >30% кириллицы, иначе en."""
    sample = " ".join(l["text"] for l in lrc_lines[:15])
    cyrillic = sum(1 for c in sample if 'Ѐ' <= c <= 'ӿ')
    return "ru" if cyrillic > len(sample) * 0.3 else "en"


def align_lrc_to_audio(lrc_lines: list[dict], audio_path: Path,
                        lang: str, total_duration: float,
                        lrc_duration: float = 0.0) -> list[dict]:
    """
    WhisperX forced alignment: привязывает текст из lrclib к реальному аудио.

    Два улучшения по сравнению с побайтовым выравниванием:
    1. Линейное масштабирование LRC-тайминга если версия из базы отличается по длине.
    2. Один сегмент на весь трек — CTC-выравнивание без ограничений LRC-окнами.
       Это устраняет накопление ошибки к концу трека.
    """
    try:
        import whisperx
    except ImportError:
        err("whisperx не установлен")
        raise

    log(f"Синхронизирую тексты к аудио (WhisperX forced alignment, язык: {lang})...")

    # 1. Масштабирование: если LRC из базы длиннее/короче реального трека — растянуть
    if lrc_duration > 0 and total_duration > 0:
        scale = total_duration / lrc_duration
        if abs(scale - 1.0) > 0.005:   # > 0.5% расхождение уже даёт дрейф к концу
            log(f"Масштабирую LRC-тайминги: ×{scale:.4f} "
                f"(LRC {lrc_duration:.1f}с → аудио {total_duration:.1f}с)")
            lrc_lines = [{"time": l["time"] * scale, "text": l["text"]}
                         for l in lrc_lines]

    # 2. Один большой сегмент на весь трек — CTC-выравнивание без ограничений LRC-окнами.
    #    Per-segment alignment растягивает слова на всё окно LRC-строки даже если
    #    реальное пение занимает лишь часть окна — это вызывает рассинхрон.
    #    Один сегмент даёт точные временные метки; слова назначаются строкам
    #    последовательно (по количеству слов в строке), минуя LRC-тайминги полностью.
    #    LRC-окна нужны только как fallback когда WhisperX вернул другое число слов.
    all_lrc_text = " ".join(l["text"] for l in lrc_lines)
    segments_align = [{"start": 0.0, "end": total_duration, "text": all_lrc_text}]

    # LRC-окна для назначения слов строкам (не для самого выравнивания)
    lrc_segments = []
    for i, line in enumerate(lrc_lines):
        start = line["time"]
        end = (lrc_lines[i + 1]["time"] - 0.05
               if i + 1 < len(lrc_lines) else min(start + 8.0, total_duration))
        lrc_segments.append({"start": start, "end": end, "text": line["text"]})

    audio = whisperx.load_audio(str(audio_path))

    try:
        align_model, align_meta = whisperx.load_align_model(language_code=lang, device="cpu")
        result = whisperx.align(segments_align, align_model, align_meta, audio, "cpu",
                                return_char_alignments=False)
    except Exception as e:
        log(f"Forced alignment не удался ({e}) — применяю равномерное распределение по строкам.")
        return _lrc_to_words_uniform(lrc_lines, total_duration)

    # Слова из глобального выравнивания — точные временные метки,
    # не искажённые принудительным растяжением на LRC-окна.
    all_aligned = sorted(
        [w for seg in result.get("segments", [])
           for w in seg.get("words", [])
           if w.get("word", "").strip() and w.get("start") is not None],
        key=lambda w: w["start"],
    )

    # Последовательное назначение: каждой LRC-строке отводится ровно столько слов,
    # сколько в ней написано. Это точнее time-window matching когда LRC-метки неверны
    # (что типично для треков с инструментальными секциями не учтёнными в lrclib).
    lrc_word_counts = [len(l["text"].split()) for l in lrc_lines]
    total_lrc_words = sum(lrc_word_counts)
    _TOLERANCE      = max(3, total_lrc_words // 15)   # ≤6.7% расхождения

    words: list[dict] = []
    fallback_count = 0

    if abs(len(all_aligned) - total_lrc_words) <= _TOLERANCE:
        # ── Основной путь: последовательное назначение ───────────────────────
        ptr = 0
        for lrc_line, wc in zip(lrc_lines, lrc_word_counts):
            line_ws = all_aligned[ptr:ptr + wc]
            ptr += wc
            if line_ws:
                entries = [{"word": w["word"].strip(),
                            "start": w["start"],
                            "end":   w.get("end", w["start"] + 0.1)}
                           for w in line_ws]
                entries[-1]["_line_end"] = True
                words.extend(entries)
            else:
                fallback_count += 1
    else:
        # ── Запасной путь: time-window matching (числа слов разошлись) ───────
        log(f"Расхождение числа слов ({len(all_aligned)} vs {total_lrc_words}) "
            f"— time-window matching.")
        word_ptr = 0
        for lrc_line, seg in zip(lrc_lines, lrc_segments):
            t_start, t_end = seg["start"], seg["end"]
            line_ws = []
            ptr = word_ptr
            while ptr < len(all_aligned):
                w = all_aligned[ptr]
                if w["start"] < t_start - 0.3:
                    ptr += 1
                    continue
                if w["start"] < t_end:
                    line_ws.append(w)
                    ptr += 1
                else:
                    break
            word_ptr = ptr
            if line_ws:
                entries = [{"word": w["word"].strip(),
                            "start": w["start"],
                            "end":   w.get("end", w["start"] + 0.1)}
                           for w in line_ws]
                entries[-1]["_line_end"] = True
                words.extend(entries)
            else:
                fallback_count += 1
                line_text = lrc_line["text"].split()
                if not line_text:
                    continue
                dur   = max(0.1, t_end - t_start)
                w_dur = dur / len(line_text)
                for j, w in enumerate(line_text):
                    entry = {"word": w,
                             "start": t_start + j * w_dur,
                             "end":   t_start + (j + 1) * w_dur}
                    if j == len(line_text) - 1:
                        entry["_line_end"] = True
                    words.append(entry)

    if fallback_count:
        log(f"Пропущено {fallback_count} строк при назначении слов.")

    if not words:
        log("Alignment вернул пустой результат — применяю равномерное распределение.")
        return _lrc_to_words_uniform(lrc_lines, total_duration)

    log(f"Синхронизировано {len(words)} слов.")
    return words


def _lrc_to_words_uniform(lrc_lines: list[dict], total_duration: float) -> list[dict]:
    """Запасной вариант: равномерно распределяет слова внутри каждой строки LRC."""
    words = []
    for i, line in enumerate(lrc_lines):
        start = line["time"]
        end = (lrc_lines[i + 1]["time"] - 0.05
               if i + 1 < len(lrc_lines) else min(start + 6.0, total_duration))
        seg_words = line["text"].split()
        if not seg_words:
            continue
        dur = (end - start) / len(seg_words)
        for j, w in enumerate(seg_words):
            entry = {"word": w, "start": start + j * dur, "end": start + (j + 1) * dur}
            if j == len(seg_words) - 1:
                entry["_line_end"] = True   # сохраняем структуру строк lrclib
            words.append(entry)
    return words


def transcribe_whisperx(audio_path: Path, lang: Optional[str]) -> list[dict]:
    try:
        import whisperx
    except ImportError:
        err("whisperx не установлен: pip install whisperx")
        sys.exit(1)

    log("Транскрибирую через WhisperX (CPU, модель medium)...")
    device = "cpu"
    model = whisperx.load_model("medium", device, compute_type="int8", language=lang)
    audio = whisperx.load_audio(str(audio_path))
    result = model.transcribe(audio, batch_size=4)

    detected_lang = result.get("language", lang or "en")
    log(f"Определён язык: {detected_lang}. Выравниваю таймкоды...")

    align_model, align_meta = whisperx.load_align_model(language_code=detected_lang, device=device)
    aligned = whisperx.align(result["segments"], align_model, align_meta, audio, device,
                             return_char_alignments=False)

    words = []
    for seg in aligned.get("segments", []):
        seg_words = [w for w in seg.get("words", []) if w.get("word", "").strip()]
        for k, w in enumerate(seg_words):
            entry = {"word": w["word"].strip(),
                     "start": w.get("start", 0.0),
                     "end":   w.get("end",   0.0)}
            if k == len(seg_words) - 1:
                entry["_line_end"] = True
            words.append(entry)

    # WhisperX капитализирует первое слово каждого нового предложения.
    # Ставим _line_end на слово перед заглавным — это граница строки каракое.
    for i in range(1, len(words)):
        w_text = words[i]["word"]
        if w_text and w_text[0].isupper() and not words[i - 1].get("_line_end"):
            words[i - 1]["_line_end"] = True

    log(f"Транскрибировано {len(words)} слов.")
    return words


# ─── 4. Генерация ASS субтитров ───────────────────────────────────────────────
#
# Раскладка (снизу вверх, PlayResY=720):
#   [Interlude]  ПРОИГРЫШ X сек — центр экрана, длинные инструментальные паузы
#   [Upcoming]   следующий текст — серый, тот же размер что Karaoke, над ним
#   [Karaoke]    текущий текст  — белый + жёлтая заливка прогресса
#
# Цвета ASS в формате &HAABBGGRR:
#   &H0000FFFF = Yellow (R=FF,G=FF,B=00) — подсвеченное слово
#   &H00FFFFFF = White                    — ещё не пропетое слово
#   &H00A0A0A0 = Grey                     — предварительная строка
#   &H0000AAFF = Orange                   — ПРОИГРЫШ, интерлюдия

_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Karaoke,Arial,40,&H0000FFFF,&H00FFFFFF,&H00000000,&HB4000000,1,0,0,0,100,100,0,0,1,3,2,2,20,20,40,1
Style: Upcoming,Arial,40,&H00A0A0A0,&H00A0A0A0,&H00000000,&HB4000000,1,0,0,0,100,100,0,0,1,2,2,2,20,20,145,1
Style: Interlude,Arial,52,&H0000AAFF,&H0000AAFF,&H00000000,&HB4000000,1,0,0,0,100,100,2,0,1,3,2,5,40,40,0,1
Style: Countdown,Arial,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,20,0,1,1,2,2,20,20,200,1
Style: IntroArtist,Arial,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,1,0,0,0,100,100,2,0,1,3,2,5,40,40,0,1
Style: IntroTitle,Arial,46,&H00C8C8FF,&H00C8C8FF,&H00000000,&H80000000,0,1,0,0,100,100,1,0,1,3,2,5,40,40,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def _ts(s: float) -> str:
    """float секунды → ASS timestamp h:mm:ss.cc"""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sc = s % 60
    return f"{h}:{m:02d}:{sc:05.2f}"


_C_FULL  = r"\c&H0000FFFF&"   # жёлтый (заполненный кружок)
_C_EMPTY = r"\c&H404040&"    # тёмно-серый (пустой кружок)


def _circle_text(filled: int, total: int = 4) -> str:
    """○○○○ → ●●○○ и т.д. с цветовыми тегами ASS"""
    parts = []
    for i in range(total):
        if i < filled:
            parts.append(f"{{{_C_FULL}}}●")
        else:
            parts.append(f"{{{_C_EMPTY}}}○")
    return "  ".join(parts)


def _add_countdown(events: list, singing_start: float, steps: int = 4) -> None:
    """Кружковый отсчёт ○○○○→●●●● в последние steps секунд перед пением"""
    t_cd = max(0.0, singing_start - steps)
    available = max(1, min(steps, int(singing_start - t_cd)))
    start_filled = steps - available
    for k in range(available):
        t0 = t_cd + k
        text = _circle_text(start_filled + k + 1, steps)
        events.append(
            f"Dialogue: 0,{_ts(t0)},{_ts(t0 + 1)},"
            f"Countdown,,0,0,0,,{text}"
        )


def _group_words_to_lines(words: list[dict],
                          max_words: int = 7,
                          gap_sec: float = 1.5) -> list[list[dict]]:
    """Разбивает слова на строки.

    Если слова содержат маркер '_line_end' (проставляется при LRC-выравнивании),
    разбивка идёт строго по нему — сохраняя структуру строк из lrclib.
    Иначе — по паузе или лимиту слов (для YouTube субтитров / Whisper).
    """
    groups, current = [], []
    use_markers = any(w.get("_line_end") for w in words)

    for i, w in enumerate(words):
        current.append(w)
        is_last = i == len(words) - 1

        if use_markers:
            too_long = len(current) >= max_words and not w.get("_line_end")
            if w.get("_line_end") or is_last or too_long:
                if current:
                    groups.append(current)
                current = []
        else:
            long_pause = not is_last and (words[i + 1]["start"] - w["end"]) > gap_sec
            if len(current) >= max_words or long_pause or is_last:
                groups.append(current)
                current = []

    return groups


def ass_karaoke_2line(words: list[dict],
                      upcoming_advance: float = 4.0,
                      intro_artist: str = "",
                      intro_title: str = "") -> str:
    """
    Генерирует ASS с двухпозиционным отображением без анимации движения.

    Дизайн (два фиксированных слота):
      Строки чередуют слоты: чётные → karaoke внизу (Y=680), нечётные → karaoke вверху (Y=575).
      Upcoming появляется В ТОМ ЖЕ слоте, что и следующее karaoke — переход без движения,
      только смена цвета (серый → белый+заливка).

    Тайминговая модель:
      _HOLD=2.0          — держать строку 2 сек после последнего слова (при длинной паузе)
      upcoming_advance=4.0 — серая строка показывается за 4 сек до начала

    TODO: добавить полупрозрачный фоновый прямоугольник под текстовую зону (backdrop)
          для улучшения читаемости на светлых видео-фонах — отложено до презентации.
    """
    _HOLD          = 2.0
    _INTERLUDE_MIN = 20.0
    _COUNTDOWN_MIN = 7.0

    # Два фиксированных слота: без \move, позиция переопределяется через \pos
    _Y_BOT = 680   # слот 0 (чётные строки: karaoke; нечётные: upcoming)
    _Y_TOP = 575   # слот 1 (нечётные строки: karaoke; чётные: upcoming)
    _X     = 640   # горизонтальный центр 1280px

    # Строки длиннее _WRAP_CHARS получают \N в mid-point.
    # Применяется одинаково к upcoming и karaoke → одинаковый перенос в обоих случаях.
    _WRAP_CHARS = 50

    lines = _group_words_to_lines(words)
    events = []

    if not lines:
        return _ASS_HEADER + "\n"

    first_start = lines[0][0]["start"]

    # ── Вспомогательные функции ───────────────────────────────────────────────

    def _split_index(line_words: list[dict]) -> int:
        """Индекс первого слова второй части (= len → нет переноса)."""
        text = " ".join(w["word"] for w in line_words)
        if len(text) <= _WRAP_CHARS:
            return len(line_words)
        mid = len(text) // 2
        best_i, best_dist, cur_len = len(line_words), float("inf"), 0
        for wi, ww in enumerate(line_words):
            cur_len += len(ww["word"]) + (1 if wi > 0 else 0)
            dist = abs(cur_len - mid)
            if dist < best_dist and 0 < wi < len(line_words) - 1:
                best_dist = dist
                best_i = wi + 1
        return best_i

    def _upcoming_text(line_words: list[dict]) -> str:
        si = _split_index(line_words)
        if si >= len(line_words):
            return " ".join(w["word"] for w in line_words)
        return (" ".join(w["word"] for w in line_words[:si])
                + "\\N"
                + " ".join(w["word"] for w in line_words[si:]))

    def _karaoke_text(line_words: list[dict], line_start: float) -> str:
        si = _split_index(line_words)
        parts = []
        cursor = line_start
        for wi, w in enumerate(line_words):
            if wi == si:
                parts.append("\\N")
            gap_cs = max(0, int((w["start"] - cursor) * 100))
            if gap_cs > 0:
                parts.append(f"{{\\k{gap_cs}}}")
            dur_cs = max(1, int((w["end"] - w["start"]) * 100))
            parts.append(f"{{\\kf{dur_cs}}}{w['word']} ")
            cursor = w["end"]
        return "".join(parts).rstrip()

    def _fade(dur_ms: int) -> str:
        """Возвращает \fad(in,out) — плавное появление и исчезновение за 500мс.
        Для коротких событий fade делится пополам, чтобы не пересекались."""
        if dur_ms <= 0:
            return ""
        half = max(1, dur_ms // 2)
        fade = min(500, half)
        return f"\\fad({fade},{fade})"

    # ── Интро: исполнитель и название ─────────────────────────────────────────
    if intro_artist or intro_title:
        intro_end = max(2.0, first_start - 5.0)
        if intro_artist:
            events.append(
                f"Dialogue: 0,{_ts(0.3)},{_ts(intro_end)},"
                f"IntroArtist,,0,0,0,,{{\\fad(800,1500)}}{intro_artist}"
            )
        if intro_title:
            t0 = 0.3 if not intro_artist else 0.8
            events.append(
                f"Dialogue: 0,{_ts(t0)},{_ts(intro_end)},"
                f"IntroTitle,,0,0,0,,{{\\fad(800,1500)}}{intro_title}"
            )

    # ── Перед первой строкой: upcoming в нижнем слоте (слот строки 0) ────────
    # Строка 0 чётная → karaoke внизу → upcoming тоже внизу → смена ролей без движения
    up_from = max(0.0, first_start - upcoming_advance)
    up_to   = first_start
    if up_to > up_from + 0.2:
        pf_dur_ms = int((up_to - up_from) * 1000)
        events.append(
            f"Dialogue: 0,{_ts(up_from)},{_ts(up_to)},"
            f"Upcoming,,0,0,0,,"
            f"{{\\pos({_X},{_Y_BOT}){_fade(pf_dur_ms)}}}"
            f"{_upcoming_text(lines[0])}"
        )

    # ── Кружковый отсчёт перед первой строкой ────────────────────────────────
    if first_start >= 2.0:
        _add_countdown(events, first_start)

    # ── Основной цикл ─────────────────────────────────────────────────────────
    # kend_by_slot[s] = момент освобождения слота s (нет конфликтов при появлении upcoming)
    kend_by_slot = [0.0, 0.0]

    for i, line in enumerate(lines):
        slot       = i % 2
        other_slot = 1 - slot
        karaoke_y  = _Y_BOT if slot == 0 else _Y_TOP
        upcoming_y = _Y_TOP if slot == 0 else _Y_BOT

        line_start = line[0]["start"]
        line_end   = line[-1]["end"]
        next_line  = lines[i + 1] if i + 1 < len(lines) else None
        next_start = next_line[0]["start"] if next_line else None
        gap        = (next_start - line_end) if next_line else None

        # Acoustic line end: cap last word at 5s to avoid WhisperX extending
        # silence into the last word's duration (CTC forced alignment artifact).
        _MAX_WORD_DUR = 5.0
        line_end_acoustic = min(line_end, line[-1]["start"] + _MAX_WORD_DUR)
        gap_acoustic = (next_start - line_end_acoustic) if next_line else None

        # kend: до каких пор держим karaoke в этом слоте
        if next_line:
            kend = next_start if gap <= _HOLD else min(line_end + _HOLD, next_start)
            kend = max(kend, line_end)
        else:
            kend = line_end + _HOLD

        # Karaoke-событие с blur-in + blur-out
        ktxt = _karaoke_text(line, line_start)
        if kend > line_start + 0.05:
            ev_dur_ms = int((kend - line_start) * 1000)
            events.append(
                f"Dialogue: 0,{_ts(line_start)},{_ts(kend)},"
                f"Karaoke,,0,0,0,,"
                f"{{\\pos({_X},{karaoke_y}){_fade(ev_dur_ms)}}}{ktxt}"
            )

        kend_by_slot[slot] = kend

        if not next_line:
            continue

        # Upcoming для следующей строки появляется в other_slot
        # (там же, где будет её karaoke → смена ролей без движения)
        # Не раньше, чем other_slot освободится от предыдущего karaoke
        upcoming_from = max(kend_by_slot[other_slot], line_start, next_start - upcoming_advance)
        upcoming_to   = next_start

        if upcoming_to > upcoming_from + 0.2:
            up_dur_ms = int((upcoming_to - upcoming_from) * 1000)
            events.append(
                f"Dialogue: 0,{_ts(upcoming_from)},{_ts(upcoming_to)},"
                f"Upcoming,,0,0,0,,"
                f"{{\\pos({_X},{upcoming_y}){_fade(up_dur_ms)}}}"
                f"{_upcoming_text(next_line)}"
            )

        # ── Инструментальные паузы ────────────────────────────────────────────
        if gap_acoustic >= _INTERLUDE_MIN:
            _add_interlude(events, line_end_acoustic + 0.5, next_start - 0.5)
        elif gap_acoustic >= _COUNTDOWN_MIN:
            _add_countdown(events, next_start - 0.5)

    return _ASS_HEADER + "\n".join(events) + "\n"


def _add_interlude(events: list, t_from: float, t_to: float) -> None:
    """
    Посекундный обратный отсчёт ПРОИГРЫШ для длинных инструментальных пауз.

    Структура:
      основной счётчик: «ПРОИГРЫШ N сек» (по 1 сек)
      последнее: blur-out за 1 сек
      финал: кружки ○○○○→●●●● в последние 4 сек до конца паузы
    """
    _TRANS    = 4   # секунд кружкового отсчёта перед концом паузы
    _FADE_OUT = 1   # секунд на fade-out ПРОИГРЫШ

    n = int(t_to - t_from)
    if n < 1:
        return

    n_regular = max(0, n - _TRANS - _FADE_OUT)

    # Основной счётчик: первое событие — fade-in 500мс, остальные — мгновенно
    for j in range(n_regular):
        t0 = t_from + j
        lead = "{\\fad(500,0)}" if j == 0 else ""
        events.append(
            f"Dialogue: 0,{_ts(t0)},{_ts(t0 + 1)},"
            f"Interlude,,0,0,0,,{lead}ПРОИГРЫШ {n - j} сек"
        )

    # Последнее событие перед отсчётом: fade-out 500мс (и fade-in если первое)
    if n > _TRANS:
        t0 = t_from + n_regular
        remaining = n - n_regular
        fad = "\\fad(500,500)" if n_regular == 0 else "\\fad(0,500)"
        events.append(
            f"Dialogue: 0,{_ts(t0)},{_ts(t0 + 1)},"
            f"Interlude,,0,0,0,,{{{fad}}}ПРОИГРЫШ {remaining} сек"
        )

    # Кружковый отсчёт ○○○○→●●●● в последние _TRANS секунды
    _add_countdown(events, t_to, steps=_TRANS)


# ─── 5. Рендер ───────────────────────────────────────────────────────────────

QUALITY_CRF = {"high": "18", "medium": "23", "low": "28"}


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    for stream in json.loads(result.stdout).get("streams", []):
        if "duration" in stream:
            return float(stream["duration"])
    return 0.0


def render(video_path: Path, instrumental: Path, ass: Path, output: Path,
           max_height: int = 720, quality: str = "medium",
           bg_image: Optional[Path] = None):
    log(f"Рендерю → {output}  ({max_height}p, quality={quality})")
    ass_escaped = str(ass).replace("\\", "/").replace(":", "\\:")
    vf = f"scale=-2:'min({max_height},ih)',ass={ass_escaped}"
    crf = QUALITY_CRF.get(quality, "23")

    # yuv420p — обязателен для совместимости с Windows/macOS без доп. кодеков
    # movflags faststart — позволяет начать воспроизведение до полной загрузки
    common_v = ["-c:v", "libx264", "-crf", crf, "-preset", "fast",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    common_a = ["-c:a", "aac", "-b:a", "192k"]

    if bg_image:
        # Режим аудио: статичная картинка + инструментал
        src_duration = get_duration(instrumental)
        subprocess.run(
            ["ffmpeg", "-y",
             "-loop", "1", "-framerate", "25", "-i", str(bg_image),
             "-i", str(instrumental),
             "-vf", vf,
             *common_v, *common_a,
             "-t", str(src_duration),
             str(output)],
            check=True,
        )
    else:
        # Режим видео: оригинальное видео + инструментал
        subprocess.run(
            ["ffmpeg", "-y",
             "-i", str(video_path), "-i", str(instrumental),
             "-vf", vf,
             "-map", "0:v:0", "-map", "1:a:0",
             *common_v, *common_a,
             str(output)],
            check=True,
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Создать каракое-версию видео или аудио",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python karaoke.py https://www.youtube.com/watch?v=...
  python karaoke.py <url> -o ~/Videos --lang ru --max-height 480 --quality low
  python karaoke.py song.mp3 --lang ru
  python karaoke.py song.flac --bg ./karaoke_bg.png --artist "Кино" --title "Группа крови"
  python karaoke.py concert.mp4 --bg ./karaoke_bg.png

Поддерживаемые форматы: mp3, flac, wav, ogg, m4a, aac, opus, mp4, mkv, avi, mov и другие.
WMA может не работать — зависит от torchaudio-бэкенда в системе.
        """,
    )
    parser.add_argument("input", metavar="URL|FILE",
                        help="Ссылка на видео или путь к локальному файлу "
                             "(.mp3, .flac, .wav, .ogg, .m4a, .aac, .mp4, .mkv, ...)")
    parser.add_argument("-o", "--output", default=".", metavar="DIR")
    parser.add_argument("--lang", default=None, metavar="LANG",
                        help="Язык для alignment/Whisper: ru, en, uk, ... (авто)")
    parser.add_argument("--no-whisper", action="store_true",
                        help="Не запускать WhisperX (только lrclib с равномерным тайм-кодом)")
    parser.add_argument("--force-whisperx", action="store_true",
                        help="Пропустить lrclib, принудительно распознать текст через WhisperX "
                             "(полезно когда lrclib даёт неверный текст или он предпочтительнее)")
    parser.add_argument("--use-yt-subs", action="store_true",
                        help="Использовать YouTube субтитры как источник текста "
                             "(только для URL, только если есть word-level timing; "
                             "обычно не нужен — lrclib+WhisperX точнее)")
    parser.add_argument("--keep-tmp", action="store_true",
                        help="Не удалять временную директорию после завершения "
                             "(полезно для отладки: там лежат vocals.wav, karaoke.ass и др.)")
    parser.add_argument("--quality", choices=["low", "medium", "high"], default="medium",
                        help="Качество видео (default: medium)")
    parser.add_argument("--max-height", type=int, default=720, metavar="PX",
                        help="Максимальная высота видео px (default: 720)")
    parser.add_argument("--bg", default=None, metavar="FILE",
                        help="Фоновое изображение (mp3: default karaoke_bg.png; видео: переопределяет исходное видео)")
    parser.add_argument("--artist", default=None,
                        help="Исполнитель (переопределяет метаданные файла)")
    parser.add_argument("--title", default=None,
                        help="Название трека (переопределяет метаданные файла)")
    parser.add_argument("--version", action="version", version=f"karaoke-cli {VERSION}")
    args = parser.parse_args()

    work_dir = Path(tempfile.mkdtemp(prefix="karaoke_"))
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log(f"Временная директория: {work_dir}")

    try:
        # 1. Получить исходный файл
        bg_image: Optional[Path] = None

        if is_url(args.input):
            # ── Режим видео (URL) ──────────────────────────────────────────
            source_path, meta = download_video(args.input, work_dir, lang=args.lang,
                                               download_subs=args.use_yt_subs)
            artist, title = parse_artist_title(meta)
            duration = get_duration(source_path)
            # --bg переопределяет исходное видео статичной картинкой
            if args.bg:
                bg_path = Path(args.bg).expanduser().resolve()
                if bg_path.exists():
                    bg_image = bg_path
                    log(f"Фон (--bg, переопределяет видео): {bg_path}")
                else:
                    log(f"Фоновый файл --bg не найден ({bg_path}) — используется исходное видео")
        else:
            # ── Режим аудио (локальный файл) ──────────────────────────────
            audio_file = Path(args.input).expanduser().resolve()
            if not audio_file.exists():
                err(f"Файл не найден: {audio_file}")
                sys.exit(1)
            source_path, meta = load_local_audio(audio_file, work_dir)
            artist, title = parse_artist_title(meta)
            duration = get_duration(source_path)

            # Фоновое изображение (--bg или default karaoke_bg.png)
            bg_path = Path(args.bg).expanduser().resolve() if args.bg else Path(__file__).parent / "karaoke_bg.png"
            if bg_path.exists():
                bg_image = bg_path
                log(f"Фон: {bg_path}")
            else:
                # Генерируем однотонный чёрный фон 1280×720
                log(f"Фоновый файл не найден ({bg_path}) — генерирую чёрный фон")
                bg_image = work_dir / "black_bg.png"
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi",
                     "-i", "color=c=black:s=1280x720:r=1",
                     "-vframes", "1", str(bg_image)],
                    check=True, capture_output=True,
                )

        # Ручное переопределение метаданных
        if args.artist:
            artist = args.artist
        if args.title:
            title = args.title

        log(f"Исполнитель: «{artist}»   Название: «{title}»")

        # 2. Отделить вокал
        instrumental, vocals = separate_vocals(source_path, work_dir)

        # 3. Тексты (приоритет: lrclib+align → YT субтитры → whisper)
        # lrclib первым: правильная структура строк + WhisperX выравнивание по аудио.
        # YouTube субтитры как запасной вариант (только если есть word-level тайминг).
        words: Optional[list[dict]] = None
        method = ""

        # 3a. lrclib + WhisperX forced alignment
        lrc_result = None if args.force_whisperx else fetch_lrclib(artist, title, duration)
        if args.force_whisperx:
            log("lrclib пропущен (--force-whisperx).")
        if lrc_result:
            lrc_lines, lrc_duration = lrc_result
            if args.no_whisper:
                words = _lrc_to_words_uniform(lrc_lines, duration)
                method = "lrclib (равномерный тайминг, --no-whisper)"
            else:
                lang = args.lang or _detect_lang_from_lrc(lrc_lines)
                words = align_lrc_to_audio(lrc_lines, vocals, lang, duration,
                                           lrc_duration)
                method = f"lrclib + WhisperX forced alignment ({lang})"

        # 3b. YouTube субтитры — только по явному запросу (--use-yt-subs)
        if words is None and is_url(args.input) and args.use_yt_subs:
            words = fetch_youtube_subtitles(work_dir, lang=args.lang)
            if words:
                method = "YouTube субтитры (word-level)"

        # 3c. WhisperX транскрипция
        if words is None:
            if args.no_whisper:
                log("Тексты не найдены и --no-whisper задан — субтитры будут пустыми.")
                words = []
                method = "пусто"
            else:
                words = transcribe_whisperx(vocals, args.lang)
                method = "WhisperX транскрипция"

        log(f"Источник текстов: {method}")

        # 4. Генерация ASS
        ass_content = (ass_karaoke_2line(words, intro_artist=artist, intro_title=title)
                       if words else _ASS_HEADER)
        ass_path = work_dir / "karaoke.ass"
        ass_path.write_text(ass_content, encoding="utf-8")
        log(f"Субтитры сгенерированы: {len(words)} слов")

        # 5. Рендер
        safe = re.sub(r'[^\w\s\-]', '', f"{artist} - {title}").strip()
        out_path = output_dir / f"{safe}.mp4"
        render(source_path, instrumental, ass_path, out_path,
               max_height=args.max_height, quality=args.quality,
               bg_image=bg_image)

        log(f"\033[32mГотово!\033[0m {out_path}")

    finally:
        if not args.keep_tmp:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            log(f"Временные файлы сохранены: {work_dir}")


if __name__ == "__main__":
    main()
