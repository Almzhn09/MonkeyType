# MonkeyType — симулятор финансового аудита Genesis

Локальная RL-среда читает синтетические счета, платежи, проводки и FX. Политика исследует записи через JSON-интерфейс и сдаёт выводы с доказательствами. Оценщик сопоставляет их со скрытым эталоном и считает P, R, F1 и награду.

## Проверка и демо

Для локального запуска нужны Linux, Python 3.11+ и системная библиотека `libseccomp.so.2`. Python-зависимости входят в стандартную библиотеку.

```sh
python3 -m audit_sim verify
python3 -m audit_sim demo --output .audit-sim/demo
```

Первая команда запускает тесты и 40 контрольных прогонов. Демо показывает, как скриптовая сверка подтверждает два вывода по открытым записям. На чистом эпизоде оно сравнивает пустой ответ с массовой пометкой. Журналы демо сохраняются в `.audit-sim/demo`.

Сборка контейнера и проверка без сети:

```sh
docker build -t genesis-audit .
docker run --rm --network none --cpus=2 --memory=4g --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec --cap-drop ALL \
  --security-opt no-new-privileges:true genesis-audit
```

## Прогон своего набора эпизодов

```sh
python3 -m audit_sim fixtures .audit-sim/dev
python3 -m audit_sim run .audit-sim/dev --mode closed --output .audit-sim/results
```

Вместо `.audit-sim/dev` можно передать один каталог эпизода или каталог набора с непосредственными подкаталогами эпизодов. По умолчанию запускаются `random`, `reconcile`, `mark_all` и `oracle`. Параметр `--policy` выбирает одну политику. `--mode train` открывает терминальную награду политике. CLI записывает `summary.json`, `reports.json` и `trajectories.jsonl`. Отчёты содержат итоговую оценку, а публичные траектории включают только ответы API и действия трёх обычных политик.

Встроенные 10 эпизодов нужны для интеграционной проверки. Для финального прогона подставьте набор train/dev из генератора Алана по тому же контракту. [Контракт данных и API](alimzhan/CONTRACT.md) · [Измерения и журналы](alimzhan/artifacts/README.md) · [Задачи Алана](alan/README.md) · [Задачи Алимжана](alimzhan/README.md)
