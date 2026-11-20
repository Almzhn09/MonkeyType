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
