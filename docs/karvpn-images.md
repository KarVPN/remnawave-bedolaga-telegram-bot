# Образы Bedolaga для KarVPN

GitHub Actions (`Build and Publish Docker Image`) собирает `linux/amd64` на
`ubuntu-24.04` и публикует приватный `ghcr.io/karvpn/remnawave-bedolaga-telegram-bot`.
VPS и локальный Mac в сборке не участвуют. Push в `main` обновляет `:main` и
`:sha-<полный SHA>`; pull request только собирает и проверяет образ.
Для обычной пересборки: Actions → Build and Publish Docker Image → Run workflow → main.
Сборка ветки вручную публикует SHA-тег, но не меняет `:main`.

## Версии и выпуск KarVPN

Единственный источник версии нашего образа — `karvpn-image-release.json`:
`upstream_version: 5.0.0` и `fork_revision: 1` дают `5.0.0-karvpn.1`.
Python-версия приложения в `pyproject.toml` остаётся upstream `5.0.0`.
За нашими изменениями следуют `5.0.0-karvpn.2`, `.3` и т. д.; обычный `fix`,
`feat` или breaking change не повышает базовую версию. При реальном обновлении
upstream измените `upstream_version` вслед за `pyproject.toml` и начните ревизию с 1.
`VERSION` и OCI labels Docker-образа содержат полную версию KarVPN.

Унаследованные release-please, Release PR Guard и Docker Hub удалены из workflows
и отключены в настройках Actions. Их старые конфигурационные JSON остаются
для синхронизации с upstream, но выпуском KarVPN не управляют.

Для выпуска:

1. В PR измените `fork_revision` (или upstream + ревизию при обновлении основы).
   Добавьте `docs/releases/<полная версия>.md`: первая строка `# <полная версия>`,
   затем короткие пункты **только изменений KarVPN с предыдущего нашего релиза**.
   Для первого выпуска подготовлен [5.0.0-karvpn.1](releases/5.0.0-karvpn.1.md).
   Старый upstream CHANGELOG сохраняется как история и в release notes не читается.
2. Слейте PR в `main`. Обычная сборка обновит main/SHA, но сама новый релиз не создаёт.
3. Откройте Actions → **KarVPN Release** → Run workflow → main.
   С `publish=false` (по умолчанию) получите только preview коротких notes в Summary;
   Docker-образ, Git-тег и GitHub Release при этом не создаются.
4. Для выпуска запустите тот же workflow с `publish=true`. Сначала проверяются
   версия, notes, принадлежность коммита main и отсутствие опубликованного релиза.
   Затем выполняются lint и общая сборка/проверка образа. После успешной публикации
   образа создаются Git-тег `v<версия>` и GitHub Release с короткими notes и digest.

Также поддерживается обычный push Git-тега `v5.0.0-karvpn.1` с коммита main:
он запускает **KarVPN Release** с той же последовательностью проверок и сборки.
Ссылаться на другую версию, выпускать из произвольной ветки, переназначать
существующий тег или перезаписывать опубликованный релиз workflow не разрешает.
Увеличьте ревизию для нового выпуска. Повторная обычная сборка main разрешена.

Релиз вызывает общий workflow сборки непосредственно через `workflow_call`.
Он не рассчитывает на запуск отдельного workflow от тега, созданного `GITHUB_TOKEN`,
и не запускает две одинаковые сборки по событию release tag. Дополнительный PAT
для публикации не нужен; используется встроенный `GITHUB_TOKEN` с правами текущих jobs.

Dockerfile копирует явный список исходников и публичных ресурсов. `.env`, данные,
дампы и ключи исключены через `.dockerignore`. Серверные секреты, Ansible Vault,
Telegram-токен и пароли баз workflow не получает. `GITHUB_TOKEN` используется
для входа в GHCR после сборки и проверки, не передаётся Docker build.
Smoke test запускается без сети и без lifespan приложения: проверяет Python-код,
профиль KarVPN и `/health`, не включает polling, планировщики или базы.

Профиль сборки переносит прежние Ansible-патчи срока Telegram Mini App initData
в образ. Публичная Actions Variable `TELEGRAM_AUTH_MAX_AGE_DAYS` по умолчанию
равна `3650`; при изменении исходных мест проверки сборка останавливается.
Widget auth не меняется. Labels `io.karvpn.bedolaga.deploy-contract=1` и
`io.karvpn.bedolaga.telegram-auth-max-age-days` позволяют плейбуку проверить,
что образ соответствует настройкам inventory.

Ansible из `vpn-infrastructure` скачивает тег, получает digest именно этого
скачивания и записывает `image: ...@sha256:...` в отдельный Compose override.
Требуется Docker Compose 2.24.4+ (`!reset` / `!override`). Существующие Compose,
PostgreSQL, Redis, имена volumes и HA overrides сохраняются; приложение запускается
с `--no-build --no-deps --pull never`. Обновление образа не является миграцией
PostgreSQL на 18 из нового upstream Compose. Для возврата кода укажите предыдущий
digest в inventory; совместимость схемы БД при откате проверяется отдельно.

Доступ VPS к приватному GHCR берётся из общего Ansible Vault (`read:packages`).
Токен действует только на время скачивания и затем удаляется с VPS вместе с
временным Docker config. В образ и его Compose-файл он не записывается.
Необходимые секреты приложения передаются на сервер в `.env` с правами `0600`.
