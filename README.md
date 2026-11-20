# MonkeyType — симулятор финансового аудита Genesis

Офлайн-среда для оценки ИИ-аудитора. Генератор создаёт вымышленные компании и вносит контролируемые ошибки. Политика читает документы и проводки через JSON API и отправляет выводы с доказательствами. Оценщик сопоставляет их со скрытым эталоном и считает точность, полноту, F1 и награду.

## Посмотреть демо на Mac

Распакуйте `genesis-submission.zip` двойным щелчком в Finder. Откройте `demo-report.html` из распакованной папки обычным браузером. В отчёте есть эпизод с двумя ошибками и чистый эпизод. Сравните политики на графике, затем откройте выводы и передвиньте ползунок по шагам расследования. Это сохранённый прогон, который открывается без установки Python, Docker и без сети.

Для нового прогона на Mac откройте Docker Desktop, затем в Терминале перейдите в папку с распакованным `Dockerfile`. Образ Python доступен для Intel и Apple Silicon. Команды соберут Linux-контейнер и покажут результаты демо в Терминале. При первой сборке Docker может скачать образ.

```sh
docker build -t genesis-audit .
docker run --rm --network none genesis-audit python -m audit_sim demo
```

## Проверка и демо

Нативный запуск агента требует Linux, Python 3.11+ и системную библиотеку `libseccomp.so.2`. Контейнер включает нужную среду. Используется стандартная библиотека Python.

```sh
python3 scripts/check.py
python3 -m audit_sim demo
```

Первая команда запускает тесты, проверяет побайтовое совпадение двух сборок данных и проводит 40 dev-прогонов плюс десять train-прогонов. Короткое демо показывает две ошибки с доказательствами в A08 и сравнивает пустой ответ с массовой пометкой на чистом dev-эпизоде. [Сценарий демо](docs/DEMO.md)

Проверка в контейнере

```sh
docker build -t genesis-audit .
docker run --rm --network none --cpus=2 --memory=4g --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec --cap-drop ALL \
  --security-opt no-new-privileges:true genesis-audit
```

## Комплект сдачи

```sh
python3 scripts/check.py --archive .audit-sim/genesis-submission.zip
python3 -m audit_sim verify-package .audit-sim/genesis-submission.zip
```

Архив содержит исходники, лицензию, Dockerfile, конфигурации, 10 train- и 10 dev-эпизодов, 20 парных вариантов, отчёты, тесты, `trajectories.jsonl` и инструкции. В `episodes/public/` находятся записи для чтения, в `episodes/private/` — файлы оценщика. Конфигурации и seed открытых наборов находятся в `configs/`. Закрытый набор для официальной проверки определяет жюри.

## Генерация и оценка другого набора

```sh
python3 -m fingen build --configs configs/train.json configs/dev.json --out .audit-sim/episodes
python3 -m fincheck verify --all .audit-sim/episodes
python3 -m audit_sim run .audit-sim/episodes --split dev --output .audit-sim/dev-results
```

Для одного эпизода с раздельными каталогами передайте `python3 -m audit_sim run PUBLIC --private PRIVATE`. Политики `random`, `reconcile`, `mark_all` и `oracle` запускаются на одних данных. `--policy` выбирает одну из них. В режиме `closed` итог остаётся у оценщика, в режиме `train` политика получает терминальную награду, которая также записывается в траекторию. Oracle работает на доверенной стороне, и его ответы исключены из публичного журнала.

[Контракт данных v1.0](docs/CONTRACT.md) · [Дизайн, награда и ограничения](docs/DESIGN.md) · [Единая проверка](scripts/README.md) · [Сессии, повторы и диагностика](docs/OPERATIONS.md)
