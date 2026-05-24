#!/bin/sh
set -eu

RAW_URL="${PLEXLOADER_RAW_URL:-https://raw.githubusercontent.com/KiMorev/tg-torrent-bot/main}"
INSTALL_DIR="${PLEXLOADER_INSTALL_DIR:-/volume1/docker/plexloader}"

say() {
  printf '%s\n' "$*"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

fail() {
  say "Ошибка: $*"
  exit 1
}

download_file() {
  url="$1"
  dest="$2"
  if have curl; then
    curl -fsSL "$url" -o "$dest"
  elif have wget; then
    wget -qO "$dest" "$url"
  else
    fail "нужен curl или wget. Установите один из них и повторите команду."
  fi
}

compose_up() {
  if docker compose version >/dev/null 2>&1; then
    docker compose up -d
  elif have docker-compose; then
    docker-compose up -d
  else
    fail "Docker найден, но docker compose недоступен. В Synology установите/обновите Container Manager."
  fi
}

run_wizard() {
  if have python3; then
    python3 scripts/setup_wizard.py --install-dir "$INSTALL_DIR"
    return
  fi

  say "Python 3 не найден. Запускаю мастер во временном Docker-контейнере python:3.12-alpine."
  if [ -r /dev/tty ]; then
    docker run --rm -it \
      --add-host=host.docker.internal:host-gateway \
      -e PLEXLOADER_WIZARD_IN_DOCKER=1 \
      -v "$INSTALL_DIR:/work" \
      -w /work \
      python:3.12-alpine \
      python scripts/setup_wizard.py --install-dir /work < /dev/tty
  else
    docker run --rm -i \
      --add-host=host.docker.internal:host-gateway \
      -e PLEXLOADER_WIZARD_IN_DOCKER=1 \
      -v "$INSTALL_DIR:/work" \
      -w /work \
      python:3.12-alpine \
      python scripts/setup_wizard.py --install-dir /work
  fi
}

check_container() {
  sleep 5
  if docker ps --filter "name=tg_torrent_drop" --filter "status=running" --format '{{.Names}}' \
    | grep -q '^tg_torrent_drop$'; then
    say "Контейнер tg_torrent_drop запущен."
    return
  fi

  say "Контейнер tg_torrent_drop не запустился. Последние логи:"
  docker logs --tail 80 tg_torrent_drop 2>/dev/null || true
  fail "исправьте ошибку из логов и повторите запуск в $INSTALL_DIR"
}

say "PlexLoader installer"
say "Папка установки: $INSTALL_DIR"

if ! have docker; then
  fail "Docker не найден. На Synology откройте Package Center и установите Container Manager."
fi

if ! docker info >/dev/null 2>&1; then
  fail "Docker установлен, но демон недоступен. Запустите Container Manager или выполните установку пользователем с правами на Docker."
fi

if ! mkdir -p "$INSTALL_DIR" 2>/dev/null; then
  say "Не удалось создать $INSTALL_DIR."
  say "Создайте папку вручную и выдайте права текущему пользователю:"
  say "  sudo mkdir -p $INSTALL_DIR"
  say "  sudo chown -R \$(whoami) $INSTALL_DIR"
  exit 1
fi

mkdir -p "$INSTALL_DIR/scripts"

say "Скачиваю compose.yaml и мастер настройки..."
download_file "$RAW_URL/compose.yaml" "$INSTALL_DIR/compose.yaml"
download_file "$RAW_URL/scripts/setup_wizard.py" "$INSTALL_DIR/scripts/setup_wizard.py"

if [ ! -d /volume1/video ]; then
  say "Медиапапка /volume1/video не найдена — отключаю необязательный /storage mount."
  grep -v '^[[:space:]]*- /volume1/video:/storage:ro[[:space:]]*$' \
    "$INSTALL_DIR/compose.yaml" > "$INSTALL_DIR/compose.yaml.tmp"
  mv "$INSTALL_DIR/compose.yaml.tmp" "$INSTALL_DIR/compose.yaml"
fi

run_wizard

if [ ! -f "$INSTALL_DIR/.env" ]; then
  fail ".env не создан, запуск контейнера отменён."
fi

cd "$INSTALL_DIR"
say "Запускаю PlexLoader..."
compose_up
check_container

say ""
say "Готово. Проверьте в Telegram:"
say "  /ping   — бот должен ответить pong"
say "  /status — должен показать Download Station"
say "  /admin  — должен открыть диагностику"
