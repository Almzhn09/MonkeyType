# MonkeyType — симулятор финансового аудита Genesis

Офлайн-среда для оценки ИИ-аудитора. Генератор создаёт вымышленные компании и вносит контролируемые ошибки. Политика читает документы и проводки через JSON API и отправляет выводы с доказательствами. Оценщик сопоставляет их со скрытым эталоном и считает точность, полноту, F1 и награду.

## Проверка и демо

Работает на Linux, macOS и Windows с Python 3.11+. Используется только стандартная библиотека Python. На Linux изоляция политики использует seccomp и требует системную библиотеку `libseccomp.so.2` (есть в большинстве дистрибутивов и в Docker-образе). На macOS и Windows используется более слабая изоляция на уровне интерпретатора (audit hook). [Подробнее](docs/DESIGN.md)

В Windows вместо `python3` используйте `py -3` или `python`.

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

## Панель проверяющего

```sh
python3 -m dashboard demo
```

Команда запускает демо через существующий CLI, готовит из его вывода очищенный набор данных в `.audit-sim/dashboard/` и открывает панель на http://127.0.0.1:8770/. В панели можно сравнить политики, открыть прогон и пройти от вывода к процитированным записям: счетам, платежам, строкам книги и курсам. Панель только читает данные. Оценка и генерация остаются в CLI.

Другие источники подключаются так:

```sh
python3 -m audit_sim run episodes --split dev --output .audit-sim/dev-results
python3 -m dashboard prepare --run .audit-sim/dev-results --episodes episodes --audience reviewer --name dev
python3 -m audit_sim release --output .audit-sim/release
python3 -m dashboard prepare --release .audit-sim/release --audience reviewer --name release
python3 -m dashboard serve
```

`--audience reviewer` показывает метрики оценщика и допускается только для синтетических сплитов demo, train, dev и pairs. `--audience public` скрывает F1, награду, TP/FP/FN и прогоны oracle. В браузер попадают только разрешённые поля и проверенные открытые записи v1.0. Файлы `truth.json`, `provenance.json` и `id_map.json`, ответы oracle и закрытые пути в браузер не передаются. Сервер слушает только 127.0.0.1 и отдаёт лишь файлы панели и подготовленный каталог.

## Игровой режим: аудитор — человек

```sh
python3 -m playground
```

Команда открывает в браузере локальную страницу (http://localhost:8765/). Выберите компанию, просматривайте книгу, счета, платежи и курсы, отмечайте ошибки с доказательствами и отправляйте ответ. Действует та же среда, что и для политик: каждая страница записей стоит шаг, лимит 100 шагов, ответ оценивает тот же оценщик. Отличие одно: у человека нет 60-секундного лимита. После отправки показываются результат, разбор каждого вывода, эталон и баллы четырёх базовых политик на той же компании. Работает без сети и сторонних библиотек. В Docker: `docker run --rm -p 8765:8765 genesis-audit python -m playground --host 0.0.0.0 --no-browser`.

## Подключение своего ИИ-агента

Встроенные политики работают в изолированном процессе без сети. LLM-агенту сеть нужна для обращения к модели, поэтому подключайте его через HTTP API игрового режима. Это та же среда и тот же оценщик: каждая страница записей стоит шаг, действует лимит в 100 шагов, ответ оценивается тем же matcher. Агент может быть написан на любом языке.

1. Запустите сервер: `python3 -m playground --no-browser`. Если агент работает в другом контейнере или на другой машине, добавьте `--host 0.0.0.0` и используйте только в доверенной сети: аутентификации нет.
2. Проверьте подключение примером: `python3 -m playground.client` выводит список компаний, `python3 -m playground.client --episode EP-0CMTEHMK3Z` проходит эпизод и печатает итог.
3. Подключите агента. На Python импортируйте `Client` из `playground/client.py` и замените `choose_findings` своей логикой. На других языках вызывайте эндпоинты напрямую.

| Метод и путь | Тело запроса | Ответ |
|---|---|---|
| `GET /api/episodes` | — | Список компаний: `id`, `split`, `company`, `base_currency`, период, размеры |
| `POST /api/start` | `{"episode": "EP-…"}` | `session` и начальное наблюдение: задача, `manifest`, `policy`, `steps_remaining` |
| `POST /api/step` | `{"session": "…", "action": {…}}` | `observation`, `steps_used`, `finished` |
| `POST /api/result` | `{"session": "…"}` | Оценка, разбор каждого вывода, эталон и баллы базовых политик. До завершения эпизода возвращает 409 |

Действия в `action` совпадают с действиями среды:

```json
{"action": "list_records", "table": "ledger", "cursor": null, "limit": 50}
{"action": "get_record", "table": "invoices", "id": "INV-2026-7149913"}
{"action": "get_document", "doc_id": "FILE-TTTDVMC"}
{"action": "calculator", "expression": "80550 * 473.0904"}
{"action": "submit", "findings": {"episode_id": "EP-0CMTEHMK3Z", "findings": [
  {"type": "fx_conversion", "affected_ids": ["FILE-TTTDVMC"], "claimed_amount_minor": 22554,
   "currency": "KZT", "evidence_ids": ["FILE-TTTDVMC", "TX-HT49KFD5", "FX:2026-11-14:EUR:KZT"],
   "explanation": "Курс не той даты"}]}}
```

Таблицы: `ledger`, `invoices`, `payments`, `fx`. Типы ошибок: `duplicate_invoice`, `payment_allocation`, `fx_conversion`, `wrong_period`. Суммы указываются в минимальных единицах валюты. ID доказательств: `row_id` для книги, `doc_id` для счетов и платежей, `FX:дата:из:в` для курсов. Ответ без ошибок — пустой список `findings`. Полные правила формата приведены в [контракте данных](docs/CONTRACT.md).

Ограничения HTTP-режима: нет 60-секундного лимита, траектории не записываются, после `submit` агент может получить эталон через `/api/result`. Поэтому он подходит для разработки и демо. Официальные метрики считайте командой `python3 -m audit_sim run`.

## Комплект сдачи

```sh
python3 scripts/check.py --output .audit-sim/submission
```

Команда записывает в новый или пустой каталог исходники, лицензию, Dockerfile, конфигурации, 10 train- и 10 dev-эпизодов, 20 парных вариантов, отчёты, тесты, `trajectories.jsonl` и инструкции. В `episodes/public/` находятся записи для чтения, в `episodes/private/` — файлы оценщика. Конфигурации и seed открытых наборов находятся в `configs/`. Материалы закрытой проверки жюри подаются отдельно.

## Генерация и оценка другого набора

```sh
python3 -m fingen build --configs configs/train.json configs/dev.json --out .audit-sim/episodes
python3 -m fincheck verify --all .audit-sim/episodes
python3 -m audit_sim run .audit-sim/episodes --split dev --output .audit-sim/dev-results
```

Для одного эпизода с раздельными каталогами передайте `python3 -m audit_sim run PUBLIC --private PRIVATE`. Политики `random`, `reconcile`, `mark_all` и `oracle` запускаются на одних данных. `--policy` выбирает одну из них. В режиме `closed` итог остаётся у оценщика, в режиме `train` терминальная награда записывается в траекторию. Oracle работает на доверенной стороне, и его ответы исключены из публичного журнала.

[Контракт данных v1.0](docs/CONTRACT.md) · [Дизайн, награда и ограничения](docs/DESIGN.md) · [Единая проверка](scripts/README.md)
