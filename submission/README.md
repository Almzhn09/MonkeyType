# Комплект сдачи Genesis

[Архив genesis-submission.zip](genesis-submission.zip) содержит исходники, лицензию, Dockerfile, инструкции, 10 train- и 10 dev-эпизодов, 20 парных вариантов, результаты проверки, обучающие траектории и автономный `train-report.html`. Список проверок и измерения находятся в [validation.json](validation.json).

В [extended-validation.json](extended-validation.json) записаны результаты длительного прогона на новых синтетических эпизодах, замеры памяти и результаты отдельной проверки распакованного архива. Чтобы повторить последние завершённые группы на текущем коде, возьмите `seed`, `start_index` и `completed_cohorts` последней фазы и передайте их в `benchmark` как `--seed`, `--start-index` и `--batches`. [Команды прогона](../docs/OPERATIONS.md#проверка-на-новых-данных)

После распаковки команда `python3 scripts/check.py` выполняет проверку. Для пересборки архива используйте `python3 scripts/check.py --archive .audit-sim/genesis-submission.zip` из корня репозитория. Команда `python3 -m audit_sim verify-package submission/genesis-submission.zip` сверяет контрольные суммы файлов.

[Сценарий выступления и текст для формы](../docs/DEMO.md). Техническое демо запускается через `python3 -m audit_sim demo`. Контрольный эпизод с двумя ошибками на контракте v1.0 можно отдельно показать командой `python3 -m audit_sim run fincheck/fixtures/a08 --policy reconcile`. Официальная отправка выполняется через канал сдачи организаторов.
