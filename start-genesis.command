#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

fail() {
  printf '%s\n' "$1" >&2
  read -r -p 'Нажмите Enter, чтобы закрыть окно. ' || true
  exit 1
}

command -v docker >/dev/null || fail 'Установите Docker Desktop и запустите этот файл снова.'
docker info >/dev/null 2>&1 || fail 'Откройте Docker Desktop, дождитесь запуска и снова откройте этот файл.'

container=genesis-admin-local
if ! docker container inspect --format '{{.State.Running}}' "$container" 2>/dev/null | grep -qx true; then
  docker build --tag genesis-admin:local . || fail 'Не удалось собрать Genesis. Посмотрите сообщение Docker выше.'
  docker run -d --rm --name "$container" --publish 127.0.0.1:8765:8765 \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec --cap-drop ALL --security-opt no-new-privileges:true \
    --mount source=genesis-admin-data,target=/state genesis-admin:local \
    python -m audit_sim serve --host 0.0.0.0 --state /state >/dev/null \
    || fail 'Не удалось запустить Genesis. Проверьте, свободен ли локальный порт 8765.'
fi

for attempt in {1..25}; do
  url=$(docker logs "$container" 2>/dev/null | awk '$1 == "Open" {print $2; exit}' || true)
  if [[ "$url" == http://127.0.0.1:8765/\?open=* ]]; then
    open "$url" || fail 'Не удалось открыть вкладку Genesis.'
    exit 0
  fi
  sleep 0.4
done
fail 'Genesis не успел запуститься. Проверьте окно контейнера genesis-admin-local в Docker Desktop.'
