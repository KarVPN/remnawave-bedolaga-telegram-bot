# CI и локальные проверки (форк KarVPN)

Документ про этот форк (`KarVPN/remnawave-bedolaga-telegram-bot`), а не про апстрим.

Коротко: **сейчас CI в форке работает** — `push` и `pull_request` запускают `Lint`,
`docker-registry`, `docker-hub` и `release-please`. Так было не всегда: до 03.10.2026
GitHub держал workflow-файлы форка выключенными (см. ниже), и все правки проверялись
только локально. Ниже — как это выглядело, как проверить состояние, как запустить линт
руками и какие падения тестов считаются фоном окружения, а не регрессией.

## Причина: GitHub не запускает workflow в форке, пока их не включат

Репозиторий — форк: `fork: true`, источник `BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot`,
создан `2026-03-24T20:55:47Z`. Если в репозитории на момент форка уже лежат
`.github/workflows/*`, GitHub **не запускает их по событиям** и показывает на вкладке
Actions баннер

> Workflows aren't being run on this forked repository. Because this repository contained
> workflow files when it was forked, we have disabled them from running on this fork.

(тот же текст GitHub показывает и в других форках — см. баг OpenJDK
[SKARA-846](https://bugs.openjdk.org/browse/SKARA-846)). Снимается он кнопкой
**«I understand my workflows, go ahead and enable them»** на вкладке Actions под
администратором репозитория.

Что было проверено 03.10.2026 (до снятия запрета):

| Проверка | Команда | Результат |
|:---|:---|:---|
| Прогоны по push | `gh api "repos/KarVPN/remnawave-bedolaga-telegram-bot/actions/runs?event=push" --jq .total_count` | `0` |
| Прогоны по pull_request | `... ?event=pull_request --jq .total_count` | `0` |
| Прогоны вручную | `... ?event=workflow_dispatch --jq .total_count` | `2` (оба — `docker-registry.yml`, успешные) |
| Проверки на PR #5 | `gh pr checks 5 -R KarVPN/remnawave-bedolaga-telegram-bot` | `no checks reported` |
| Права токена | `gh api repos/KarVPN/remnawave-bedolaga-telegram-bot --jq .permissions` | `admin: false` |
| Настройки Actions | `gh api repos/KarVPN/remnawave-bedolaga-telegram-bot/actions/permissions` | `403` (нужен admin) |

Ключевое доказательство, что дело было **не в триггерах в YAML**: `337c3040`
(2026-10-03 02:17:36 +03) — прямой push в `main`, его родитель — merge PR #3. В этом
коммите `docker-registry.yml` уже содержал `on.push.branches: [main]`, и всё равно ни
одного прогона не появилось; через 26 секунд тот же workflow запустили руками
(`gh workflow run docker-registry.yml --ref main`) — и он прошёл штатно.

## Как это выглядит сейчас (проверено)

Запрет был снят между последним «молчащим» push (`337c3040`) и проверкой в ~12:00Z
03.10.2026: к этому моменту все пять workflow отдавались API со `state: active`
(у workflow, который GitHub ещё не разрешил запускать в форке, состояние —
`disabled_fork`, а не `active`). Поведением это подтвердилось сразу:

| Время (UTC) | Событие | Прогоны |
|:---|:---|:---|
| 12:05:52 | push ветки `ticket/152-ci-triggers` | `Lint` (push, ветка) → **failure** |
| 12:06:29 | push в `main` (`3b5d9ddd`) | `Lint`, `BedolagaBot` (docker-hub), `Build and Publish Docker Image` (docker-registry), `Release Please` |

Важная деталь: в `main` в тот момент лежал **исходный** `lint.yml` с
`branches: ['**']` — и `Lint` на push всё равно сработал. Значит фильтры веток в
триггерах были исправны, а причина была ровно одна — форк-запрет.

Если прогоны снова пропали — смотреть ту же вкладку Actions на баннер и поле `state`
у workflow; `disabled_fork` означает, что запрет вернулся.

## Линт вручную

Полезно, когда нужно перезапустить проверку без нового коммита (`workflow_dispatch`
добавлен в `lint.yml`):

```bash
gh workflow run lint.yml --ref <ветка>
gh run list --workflow lint.yml --limit 3
gh run watch <run-id>          # или: gh run view <run-id> --log-failed
```

Сборка образа — так же: `gh workflow run docker-registry.yml --ref main`.

`Lint` вызывает и `release.yml` (`jobs.lint.uses`), поэтому в `lint.yml` добавлен
триггер `workflow_call` — без него вызов переиспользуемого workflow невалиден.

## Локальный прогон без CI

```bash
uv sync --group dev        # создаёт .venv как в CI (ruff берётся из uv.lock)

.venv/bin/python -m pytest tests/ -q --continue-on-collection-errors
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

`--continue-on-collection-errors` обязателен: без него pytest обрывает прогон на трёх
файлах, которые не собираются на macOS (см. ниже), и до остальных тестов дело не доходит.

Если в рабочем каталоге параллельно работает другой агент/ветка, прогон удобно делать
в отдельном worktree, чтобы не мешать: `git worktree add ../bot-152 main`.

## Baseline падений окружения

Это окружение, а не регрессия кода. Сравнивать свой результат нужно с **чистым `main`**,
а не с нулём.

| Прогон | passed | failed | errors |
|:---|:---|:---|:---|
| чистый `main` (до #78), из тикета #152 | 357 | 78 | 14 |
| `main` = `ee2deac2` (после #78), из тикета #152 | 372 | 78 | 14 |
| `main` = `ee2deac2`, воспроизведено локально | **372** | **78** | **17** строк `ERROR` = 14 setup-ошибок + 3 ошибки сбора |

Разница 14 → 17 — это ровно те три файла, которые не собираются: pytest печатает их как
`ERROR` отдельно от `failed`, а `--continue-on-collection-errors` добавляет их к общему счёту.

Причины падений:

* **14 setup-ошибок** — фикстуры БД: нет поднятых PostgreSQL/Redis
  (`tests/crud/test_promocode_crud.py`, `tests/integration/test_promocode_promo_group_flow.py`,
  `tests/services/test_promocode_service.py`);
* **3 ошибки сбора** — `BACKUP_LOCATION` по умолчанию `/app/data/backups`
  (`app/config.py`), а на macOS корень только для чтения:
  `OSError: [Errno 30] Read-only file system: '/app'`
  (`tests/test_miniapp_payments.py`, `tests/test_webapi_subscriptions_tariff.py`,
  `tests/webserver/test_unified_app.py`).

## Долг форматирования: закрыт в PR #7

Пока запрет был снят, `Lint` на `main` (`ee2deac2`, `3b5d9ddd`) падал: наш снимок `app/**`
отстал от апстрима, а `uv.lock` тянет ruff 0.14.14. Что показывал прогон:

```
ruff check .            → 6 ошибок (ruff 0.14.14 из uv.lock)
  app/cabinet/routes/balance.py:3:1: I001
  app/cabinet/routes/subscription.py:3:1: I001
  app/handlers/balance/yookassa.py:1:1: I001
  app/handlers/subscription/tariff_purchase.py:2012:5: F841 `texts`
  app/handlers/subscription/tariff_purchase.py:2029:5: F841 `actual_device_limit`
  app/services/yookassa_receipt_contact.py:1:1: I001

ruff format --check .   → 4 файла
  app/cabinet/routes/subscription.py
  app/handlers/balance/yookassa.py
  app/handlers/subscription/tariff_purchase.py
  app/services/pricing_engine.py
```

В PR #7 это приведено к `ruff format` (поведение не меняется):

* `ruff format` по четырём файлам — перенос длинных выражений и условий, кавычки в
  f-строках, пустые строки перед определением, склейка `state.update_data` в одну строку;
* `ruff check --fix` — порядок импортов в четырёх файлах;
* `F841` в `select_tariff_extend_period`: `get_texts(...)` оставлен вызовом без
  присваивания (так предлагает сам ruff — вызов читает файлы локалей), чистое
  присваивание `actual_device_limit` удалено. Дифф `--unsafe-fixes` перед применением
  просмотрен.

Проверка: `ruff check .` и `ruff format --check .` по репозиторию проходят чисто, список
падающих тестов до и после правки совпадает по id (78 failed / 372 passed / 17 errors).

## Что в форке красное и почему (03.10.2026)

| Workflow | Итог | Причина |
|:---|:---|:---|
| `Build and Publish Docker Image` | success | образ публикуется в `ghcr.io`, секреты не нужны |
| `Lint` | success | после PR #7 |
| `BedolagaBot` (`docker-hub.yml`) | failure | падает на шаге «Login to Docker Hub»: в форке нет секретов `DOCKER_USERNAME` и `DOCKER_PASSWORD` |
| `Release Please` | failure | `release-please failed: GitHub Actions is not permitted to create or approve pull requests` |

Что с этим делать (решает владелец):

1. `Release Please` / `Release`: **Settings → Actions → General → Workflow permissions →
   «Allow GitHub Actions to create and approve pull requests»** — без этого релизный
   workflow не сможет открыть PR.
2. `BedolagaBot`: либо добавить секреты Docker Hub в форк, либо пропускать публикацию,
   когда секретов нет, либо отключить workflow в форке — сейчас он красный всегда.
   Форк публикует образ в `ghcr.io`, поэтому Docker Hub ему, скорее всего, не нужен.
3. Если прогоны снова пропадут — проверить, что на вкладке Actions не вернулся баннер
   форка, а `state` у workflow не стал `disabled_fork`.
