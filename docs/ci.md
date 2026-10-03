# CI и локальные проверки (форк KarVPN)

Документ про этот форк (`KarVPN/remnawave-bedolaga-telegram-bot`), а не про апстрим.
Коротко: **автоматические прогоны Actions в форке не работают** — GitHub отключает
workflow-файлы, которые были в репозитории на момент форка. Линт запускается вручную,
образ публикуется вручную, тесты гоняются локально. Ниже — факты, команды и baseline
падений окружения, чтобы их не приняли за регрессию.

## Почему Actions не срабатывает сам

Репозиторий — форк: `fork: true`, источник `BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot`,
создан `2026-03-24T20:55:47Z`. На момент форка в нём уже были `.github/workflows/*`,
и GitHub такие workflow в форке **не запускает по событиям**: в веб-интерфейсе на вкладке
Actions висит баннер

> Workflows aren't being run on this forked repository. Because this repository contained
> workflow files when it was forked, we have disabled them from running on this fork.

(тот же текст GitHub показывает и в других форках — см. баг OpenJDK
[SKARA-846](https://bugs.openjdk.org/browse/SKARA-846)).

Проверенные факты на `main`:

| Проверка | Команда | Результат |
|:---|:---|:---|
| Прогоны по push | `gh api "repos/KarVPN/remnawave-bedolaga-telegram-bot/actions/runs?event=push" --jq .total_count` | `0` |
| Прогоны по pull_request | `... ?event=pull_request --jq .total_count` | `0` |
| Прогоны вручную | `... ?event=workflow_dispatch --jq .total_count` | `2` (оба — `docker-registry.yml`, успешные) |
| Состояние workflow | `gh workflow list --all` | все 5 (`Lint`, `docker-registry`, `docker-hub`, `release-please`, `release`) — `active` |
| Проверки на PR #5 | `gh pr checks 5 -R KarVPN/remnawave-bedolaga-telegram-bot` | `no checks reported` |

Ключевое доказательство, что дело не в триггерах в YAML:

* `337c3040` (2026-10-03 02:17:36 +03) — **прямой push в `main`**, его родитель —
  merge PR #3. В этом коммите `docker-registry.yml` уже содержал
  `on.push.branches: [main]`, и всё равно ни одного прогона не появилось;
* через **26 секунд** тот же workflow запустили руками (`gh workflow run docker-registry.yml
  --ref main`) — и он прошёл штатно.

То есть события `push`/`pull_request` в форке не доходят до Actions вообще, при этом
ручной запуск (`workflow_dispatch`) работает. `state: active` в API форк-запрет не
отражает, поэтому «workflow активен» здесь ничего не значит.

Права: у нашей учётки в репозитории `admin: false`, поэтому
`GET /repos/KarVPN/remnawave-bedolaga-telegram-bot/actions/permissions` отдаёт
**403** («You must have repository read permissions or have the repository Actions
policies fine-grained permission»), `orgs/KarVPN/actions/permissions` — тоже 403.
`gh workflow enable lint.yml` выполняется без ошибки, но это no-op: workflow и так
числится активным, а форк-запрет через API с нашими правами не виден и не снимается.

### Что должен сделать владелец

1. Открыть https://github.com/KarVPN/remnawave-bedolaga-telegram-bot/actions под
   аккаунтом с правами администратора репозитория и нажать зелёную кнопку
   **«I understand my workflows, go ahead and enable them»**.
2. Проверить **Settings → Actions → General**: должны стоять «Allow all actions and
   reusable workflows» и Actions не отключены; при необходимости проверить
   политику на уровне организации `KarVPN`.
3. Убедиться, что запрет снят: `push` в любую ветку (или merge в `main`) должен
   породить прогон `Lint`, а `gh api "repos/KarVPN/remnawave-bedolaga-telegram-bot/actions/runs?event=push" --jq .total_count`
   — стать больше нуля.

## Линт вручную (пока Actions закрыт)

```bash
gh workflow run lint.yml --ref <ветка>          # workflow_dispatch добавлен в lint.yml
gh run list --workflow lint.yml --limit 3
gh run watch <run-id>                            # или: gh run view <run-id> --log-failed
```

Сборка образа — так же: `gh workflow run docker-registry.yml --ref main`.

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

## Известный долг: `Lint` на `main` красный

Даже после включения Actions первый прогон `Lint` на `main` (`ee2deac2`) будет падать —
это уже существующий долг, все файлы в `app/**`:

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

Правки в `app/**` в этот PR не входят (зона тикета #163), поэтому линт останется красным
до отдельного коммита по этим файлам.
