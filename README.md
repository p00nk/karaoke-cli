# karaoke-cli

Конвертирует музыкальное видео или аудиофайл в karaoke-версию с синхронизированными субтитрами.

```
python karaoke.py song.mp3 --lang ru
python karaoke.py https://www.youtube.com/watch?v=... --max-height 480
```

## Что получается на выходе

MP4-видео с двухстрочными ASS-субтитрами:
- **Активная строка** — белый текст с жёлтой заливкой слово за словом
- **Следующая строка** — серый текст, появляется заранее
- **Обратный отсчёт** `●○○○` перед каждым куплетом после паузы
- **ПРОИГРЫШ** с таймером при длинных инструментальных вставках
- Плавное появление и исчезновение (fade in/fade out)

## Зависимости

- Python 3.10+
- ffmpeg
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — скачивание видео
- [demucs](https://github.com/facebookresearch/demucs) — отделение вокала
- [whisperx](https://github.com/m-bain/whisperX) — транскрипция и forced alignment
- [requests](https://pypi.org/project/requests/), [mutagen](https://pypi.org/project/mutagen/)

## Установка

```bash
git clone https://github.com/p00nk/karaoke-cli.git
cd karaoke-cli
sudo bash install.sh        # ffmpeg + venv + PyTorch CPU + все зависимости
source .venv/bin/activate
```

> `install.sh` настроен для Ubuntu 24.04 / WSL2 без GPU (PyTorch CPU). Для CUDA замени строку установки torch на нужный индекс.

## Использование

```bash
python karaoke.py <URL|файл> [опции]
```

### Примеры

```bash
# YouTube-видео → karaoke
python karaoke.py https://www.youtube.com/watch?v=...

# MP3-файл, явно указать язык и разрешение
python karaoke.py song.mp3 --lang ru --max-height 480

# Заменить фон для видеоклипа статичной картинкой
python karaoke.py https://www.youtube.com/watch?v=... --bg ./my_bg.png

# Принудительно использовать распознавание вместо lrclib
python karaoke.py song.mp3 --lang ru --force-whisperx

# Указать метаданные вручную (если в файле неверные теги)
python karaoke.py track.mp3 --artist "Кино" --title "Группа крови"

# Просто скачать видео без генерации субтитров (YouTube, VK, Rutube, ...)
python karaoke.py https://www.youtube.com/watch?v=... --only-download -o ~/Downloads
python karaoke.py https://vkvideo.ru/video... --only-download -o ~/Downloads
python karaoke.py https://rutube.ru/video/... --only-download -o ~/Downloads
```

### Параметры

| Параметр | По умолчанию | Описание |
|---|---|---|
| `--lang LANG` | авто | Язык для WhisperX: `ru`, `en`, `uk`, ... Авто-определение по кириллице в LRC. |
| `--max-height PX` | `720` | Максимальная высота выходного видео |
| `--quality` | `medium` | Качество видео: `low`, `medium`, `high` |
| `--bg FILE` | авто | Фоновое изображение. Для MP3 — заменяет дефолтный `karaoke_bg.png`; для видео/URL — переопределяет исходное видео статичной картинкой |
| `--artist` | из тегов | Исполнитель (переопределяет метаданные файла) |
| `--title` | из тегов | Название трека (переопределяет метаданные файла) |
| `--only-download` | выкл | Только скачать видео по URL в `--output` и выйти, без субтитров (поддерживает YouTube, VK, Rutube и любые источники yt-dlp) |
| `--force-whisperx` | выкл | Пропустить lrclib, принудительно распознать текст через WhisperX |
| `--no-whisper` | выкл | Не запускать WhisperX (только lrclib с равномерным тайм-кодом) |
| `--use-yt-subs` | выкл | Использовать YouTube субтитры (только если есть word-level timing) |
| `--keep-tmp` | выкл | Сохранить временную директорию после завершения (для отладки) |
| `--version` | — | Вывести версию |

## Как работает пайплайн

```
Вход (URL / аудио / видео)
    │
    ├─ URL → yt-dlp → скачать аудио/видео
    │
    ├─ demucs → отделить вокал (vocals.wav)
    │
    ├─ Источник текста (приоритет):
    │     1. lrclib.net — поиск по исполнителю + названию
    │     2. --force-whisperx → сразу WhisperX транскрипция
    │
    ├─ WhisperX forced alignment — точные тайм-коды слов
    │     • Глобальный CTC (один сегмент = весь трек)
    │     • Последовательное назначение слов по LRC-строкам
    │     • Перенос строк по заглавным буквам (WhisperX-режим)
    │
    ├─ Генерация ASS-субтитров
    │     • Двухслотовая анимация (Y=680 / Y=575)
    │     • \kf заливка слово за словом
    │     • \fad fade in/out для всех событий
    │     • Обратный отсчёт и ПРОИГРЫШ при паузах
    │
    └─ ffmpeg → сборка MP4
```

## Поддерживаемые форматы

**Входные файлы:** mp3, flac, wav, ogg, m4a, aac, opus, mp4, mkv, avi, mov и любой формат, читаемый demucs/ffprobe.

**URL:** YouTube, а также любые источники, поддерживаемые yt-dlp.

> WMA может не работать — зависит от torchaudio-бэкенда в системе.

## Версии

См. [CHANGELOG.md](CHANGELOG.md).
