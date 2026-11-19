# MonkeyType

Genesis FIN_CASE 2026: автономная RL-среда для поиска бухгалтерских расхождений. Генератор
строит синтетическую компанию за один отчётный период (книга проводок, счета, платежи,
курсы валют), вносит в неё ошибки с записанной причиной и выдаёт эталон с допустимыми
наборами доказательств. Агент должен найти расхождения по открытым записям.

Команда: Алан (генератор, эпизоды, контейнер) и Алимжан (API среды, оценщик, политики).
Планы: [`alan/README.md`](alan/README.md), [`alimzhan/README.md`](alimzhan/README.md).
Требования кейса: [`docs/spec/genesis-excerpt.md`](docs/spec/genesis-excerpt.md).
Контракт данных: [`docs/CONTRACT.md`](docs/CONTRACT.md). Записка о дизайне:
[`docs/DESIGN.md`](docs/DESIGN.md).

## Карта репозитория

| Путь | Что там |
|---|---|
| `fingen/` | Генератор: конфигурация, скрытая история, внедрение ошибок (`inject/`), экспорт, парные эпизоды, CLI |
| `fincheck/` | Независимый проверяющий (не импортирует `fingen`): загрузчик, пересчёт, reference-reconciler, reference-matcher, отчёт `verify` |
| `configs/` | `train.json`, `dev.json` (по 10 конфигураций и seed), `examples/` (шаблоны) |
| `episodes/` | Готовые эпизоды: `public/` и `private/` по `train`, `dev`, `pairs` |
| `tests/` | `unittest`-тесты генератора, проверяющего и наборов эпизодов |
| `scripts/check.py` | Единая команда проверки (см. [`scripts/README.md`](scripts/README.md)) |
| `docs/` | `CONTRACT.md`, `DESIGN.md`, `spec/genesis-excerpt.md` |
| `Dockerfile` | Образ `python:3.11.11-slim-bookworm`, запуск `python scripts/check.py` |
| `alan/`, `alimzhan/` | Планы работ участников |

## Требования

Python 3.11, только стандартная библиотека (`requirements.txt` пуст). Сеть, платные API и
GPU не нужны. Предлагаемые лимиты кейса: 2 CPU, 4 ГБ RAM, 60 секунд на эпизод.

## Быстрый старт

Контейнер (единая команда проверки внутри — `python scripts/check.py`):

```
docker build -t fincase .
docker run --rm --network none --cpus 2 --memory 4g fincase
```

Локально, без Docker:

```
python3.11 scripts/check.py            # все стадии
python3.11 scripts/check.py --quick    # без стадии fresh-gen
python3.11 scripts/check.py --verbose  # показать вывод успешных стадий
python3.11 -m unittest discover -s tests -t .
```

Код выхода `check.py` не равен нулю, если хотя бы одна стадия получила `FAIL`.

## Генерация эпизодов

```
# один эпизод из конфигурации (--index N выбирает элемент списка конфигураций)
python3.11 -m fingen generate --config configs/examples/all_types.json --out /tmp/ep

# все эпизоды train и dev + парные (перестановка и смена ID)
python3.11 -m fingen build --configs configs/train.json configs/dev.json --out episodes --pairs

# парный эпизод вручную
python3.11 -m fingen pair --public P --private Q --out DIR --split pairs --seed 7

# воспроизводимость: два прогона, побайтовое сравнение
python3.11 -m fingen selfcheck --config configs/examples/all_types.json --out /tmp/sc [--pairs]
```

Проверка (код выхода 0 только если все проверки пройдены):

```
python3.11 -m fincheck verify --all episodes          # public/<split>/* + private/<split>/*
python3.11 -m fincheck verify --public P --private Q  # один эпизод
python3.11 -m fincheck reconcile --public P -o answer.json   # ответ reference-reconciler
python3.11 -m fincheck match --public P --private Q --submission answer.json
```

## Структура эпизода

```
episodes/
  public/<split>/<episode_id>/    manifest.json policy.json ledger.csv invoices.jsonl payments.jsonl fx.csv
  private/<split>/<episode_id>/   truth.json provenance.json [id_map.json у парных]
```

* `public/` монтируется в процесс API только для чтения; агент видит только его.
* `private/` получает только процесс оценщика; в процесс агента он не монтируется.
  Seed хранится только в `provenance.json` и наружу не выдаётся.
* Имена файлов не зависят от наличия или типа ошибок. Форматы — в
  [`docs/CONTRACT.md`](docs/CONTRACT.md).
* В Docker-образе для этого предусмотрены точки монтирования `/data/public` (`:ro`) и
  `/data/private` (см. комментарий в `Dockerfile`); сам API среды пока не реализован.

## Пример запуска

Реальный вывод `python3.11 scripts/check.py --quick` (19.11.2026, Python 3.11.18):

```
[tests] PASS (10.6s)
[repro] PASS (0.2s)
[verify-episodes] PASS (1.1s)
[fresh-seed] PASS (0.3s)
[fresh-gen] SKIP (--quick)
[env] SKIP (teammate package not present)
peak RSS (self+children): 61 MB
RESULT: OK
```

Что означают строки:

* `tests` — `unittest`-тесты в `tests/` (генератор, внедрение ошибок, проверяющий, наборы эпизодов).
* `repro` — `fingen selfcheck` для `configs/examples/all_types.json`: два независимых прогона,
  все файлы побайтово совпадают.
* `verify-episodes` — `fincheck verify --all episodes`: проверка всех готовых эпизодов
  (train, dev, pairs): схема, равенства до ошибок, суммы и достаточность доказательств, утечки.
* `fresh-seed` — ещё не использованный seed и сочетание ошибок, которого нет в train/dev:
  генерация, `fincheck verify` и `selfcheck` (имитация закрытого прогона).
* `fresh-gen` — пропущена из-за `--quick`; без флага генерирует каждый
  `configs/examples/*.json`, проверяет и печатает время и пиковую память против лимитов.
* `env` — вызывает `python -m fincase_env.check` (имя меняется через `FINCASE_ENV_CHECK`),
  если пакет среды Алимжана есть; сейчас его нет, поэтому `SKIP` (не ошибка).
* `peak RSS` — пиковая память самого скрипта и дочерних процессов.
* `RESULT: OK` — ни одна стадия не завершилась `FAIL`.

## Статус

Реализовано (сторона Алана):

* генератор: скрытая согласованная история, четыре типа ошибок, два сочетания, отвлекающие
  случаи, чистые эпизоды (`fingen/`);
* независимый проверяющий `fincheck/` (схема, пересчёт, достаточность доказательств,
  утечки, reference-reconciler и reference-matcher для проверки достижимости эталона);
* парные эпизоды (`pair`, `id_map.json`) и `selfcheck`;
* 10 train + 10 dev эпизодов и 20 парных в `episodes/`;
* `Dockerfile` и `scripts/check.py`. Реальная сборка образа в этой среде не выполнялась
  (команды `docker build/run` не проверены); Dockerfile пока не закреплён по digest.

Ожидает стороны Алимжана (в репозитории отсутствует):

* API среды `reset`/`step`, лимит 100 шагов, изоляция процессов;
* общий matcher и награда по §4–5 кейса (в `fincheck/matcher_stub.py` лежит только
  reference-реализация для проверки эталона, не итоговый оценщик);
* четыре политики организаторов (случайная, простая сверка, «пометить всё», oracle);
* `trajectories.jsonl` и демо.

## Лицензия

MIT, см. [`LICENSE`](LICENSE).
