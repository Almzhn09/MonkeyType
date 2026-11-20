# Комплект сдачи Genesis

[Архив genesis-submission.zip](genesis-submission.zip) содержит исходники, лицензию, Dockerfile, инструкции, 10 train- и 10 dev-эпизодов, 20 парных вариантов, результаты проверки и обучающие траектории. После распаковки откройте `start-genesis.command` на Mac с запущенным Docker Desktop, чтобы создавать вымышленные компании в браузере. Самостоятельные `demo-report.html` и `train-report.html` показывают сохранённые прогоны. Список проверок и измерения находятся в [validation.json](validation.json).

В [review-validation.json](review-validation.json) сохранена расширенная проверка генератора и политик, проведённая до добавления графиков. Группы из `fresh_policy_cohorts` можно повторить с указанными `seed`, `start_index` и `completed_cohorts` через `benchmark --seed … --start-index … --batches …`. Замеры более ранней сборки сохранены в [extended-validation.json](extended-validation.json). [Команды прогона](../docs/OPERATIONS.md#проверка-на-новых-данных)

После распаковки команда `python3 scripts/check.py` выполняет проверку. Для пересборки архива используйте `python3 scripts/check.py --archive .audit-sim/genesis-submission.zip` из корня репозитория. Команда `python3 -m audit_sim verify-package submission/genesis-submission.zip` сверяет контрольные суммы файлов.

[Сценарий выступления и текст для формы](../docs/DEMO.md). Техническое демо запускается через `python3 -m audit_sim demo`. Контрольный эпизод с двумя ошибками на контракте v1.0 можно отдельно показать командой `python3 -m audit_sim run fincheck/fixtures/a08 --policy reconcile`. Официальная отправка выполняется через канал сдачи организаторов.
