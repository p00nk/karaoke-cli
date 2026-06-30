# karaoke-cli — руководство разработчика

## Быстрый старт

```bash
source .venv/bin/activate
python karaoke.py song.mp3 --lang ru --keep-tmp
python karaoke.py https://www.youtube.com/watch?v=... --max-height 480
```

Временная директория (`--keep-tmp`) — в `/tmp/karaoke_XXXXXX/`, содержит `vocals.wav` и `karaoke.ass`.

## Стек

| Компонент | Библиотека | Роль |
|---|---|---|
| Разделение вокала | `demucs` (Facebook) | Вырезает вокал из микса → `vocals.wav` |
| Транскрипция/выравнивание | `whisperx` | CTC forced alignment → тайм-коды слов |
| Скачивание | `yt-dlp` | YouTube, VK, Rutube, любые источники |
| Видео/аудио | `ffmpeg` (CLI) | Декодирование, наложение субтитров, MP4 |
| Тексты | lrclib.net REST API | LRC (построчные тайм-коды) по artist+title |
| Теги аудио | `mutagen` | Чтение ID3/FLAC/OGG тегов (CP1251-safe) |
| HTTP | `requests` | Запросы к lrclib.net |
| Субтитры | ASS (текст) | Генерируется вручную в `_build_ass()` |

Python 3.10+. Без GPU — всё работает на CPU (PyTorch CPU-only build).

## Архитектура пайплайна

```
Вход (URL / локальный файл)
  │
  ├─ URL → yt-dlp → аудио/видео в tmpdir
  │
  ├─ ffmpeg → audio.wav (16 kHz, mono) для demucs
  │
  ├─ demucs → vocals.wav  (htdemucs, отделение вокала)
  │
  ├─ lrclib.net → LRC (artist + title из тегов / CLI-флагов)
  │     fallback: --force-whisperx → whisperx.transcribe()
  │
  ├─ align_lrc_to_audio()          ← ключевая функция синхронизации
  │     1. Масштабирование LRC-таймингов под длину аудио
  │     2. Разбиение на вокальные секции (LRC-зазор > 15 с)
  │     3. Для каждой секции:
  │           a. Аудио-окно [sec_start−5 с … next_sec_start−5 с]
  │           b. whisperx.align() — CTC на vocals.wav фрагменте
  │           c. Последовательное назначение слов LRC-строкам
  │              (по числу слов; независимо для каждой секции)
  │           d. Fallback: time-window matching при расхождении счёта
  │     4. Пост-обработка: _INTRA_GAP_SPLIT ≥5 с → доп. разбивка строк
  │
  ├─ _build_ass()                  ← генерация ASS-субтитров
  │     • Двухслотовая схема: чётные строки Y=680, нечётные Y=575
  │     • \kf — заливка слова, \k — пауза, \fad() — fade in/out
  │     • Обратный отсчёт (●○○○) при паузах ≥7 с
  │     • Баннер ПРОИГРЫШ + таймер при паузах ≥20 с
  │
  └─ ffmpeg → MP4 (видео + ASS hardcode)
```

## Ключевые функции

| Функция | Строки | Что делает |
|---|---|---|
| `main()` | ~1050 | Точка входа, argparse, orchestration |
| `align_lrc_to_audio()` | ~390 | Синхронизация LRC→аудио (секционный CTC) |
| `_build_ass()` | ~700 | Генерация ASS-файла субтитров |
| `ass_karaoke_2line()` | ~800 | Разбивка слов на двухстрочные события |
| `_karaoke_text()` | ~747 | Формирование `\kf`/`\k`-разметки строки |
| `_fade()` | ~762 | Fade-in/out параметры для события |
| `_add_countdown()` | ~850 | Обратный отсчёт перед куплетом |
| `_group_words_to_lines()` | ~640 | Группировка слов по `_line_end` маркерам |

## Параметры синхронизации (константы в коде)

```python
_SECTION_GAP    = 15.0  # LRC-зазор (с) → граница вокальной секции
_SECTION_MARGIN = 5.0   # буфер аудио до/после секции
_INTRA_GAP_SPLIT = 5.0  # внутристрочный зазор → доп. разбивка строки
_MAX_FILL        = 4.0  # макс. длина \kf-заливки слова (с)
_COUNTDOWN_MIN   = 7.0  # мин. пауза для обратного отсчёта
_INTERLUDE_MIN   = 20.0 # мин. пауза для баннера ПРОИГРЫШ
_TOLERANCE       = max(3, total_words // 15)  # допуск при сравнении счёта слов
```

## Известные ограничения

- **CTC non-determinism**: whisperx forced alignment может давать разные результаты при повторных запусках на одном аудио. Секционный подход минимизирует, но не устраняет.
- **LRC drift**: тайм-коды из lrclib.net иногда расходятся с реальным аудио на 5–20 с. Последовательное назначение слов (не time-window) устойчиво к этому.
- **WMA**: может не работать в зависимости от torchaudio-бэкенда.
- **demucs**: нет GPU → ~3–5 мин на трек 4 мин при CPU.

## Тестирование

Ручное:
```bash
# Полный прогон с сохранением tmp
python karaoke.py /path/to/song.mp3 --lang ru --keep-tmp --output /tmp/out/

# Только скачать (без субтитров)
python karaoke.py https://youtu.be/... --only-download -o /tmp/
```

Автотестов нет. Проверка синхронизации — визуально по выходному MP4.

## Версии

Формат: `MAJOR.MINOR` (без patch). Изменения в `CHANGELOG.md`.
Текущая: см. `VERSION` в начале `karaoke.py`.
