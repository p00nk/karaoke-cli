#!/bin/bash
# Установка karaoke-cli на Ubuntu 24.04 / WSL2 (AMD CPU, без GPU)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[1/4] Системные пакеты..."
apt-get update -q
apt-get install -y ffmpeg python3-pip python3-venv

echo "[2/4] Виртуальное окружение..."
python3 -m venv .venv

echo "[3/4] PyTorch CPU (для AMD/WSL2 без GPU)..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu -q

echo "[4/4] Зависимости проекта..."
.venv/bin/pip install yt-dlp demucs requests whisperx -q

echo ""
echo "Установка завершена!"
echo "Активируй окружение: source .venv/bin/activate"
echo "Запуск примера:       python karaoke.py <url>"
