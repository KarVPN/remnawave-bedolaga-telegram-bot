# Образы Bedolaga для KarVPN

GitHub Actions (`Build and Publish Docker Image`) собирает `linux/amd64` на
`ubuntu-24.04` и публикует `ghcr.io/karvpn/remnawave-bedolaga-telegram-bot`.
VPS и локальный Mac в сборке не участвуют. Push в `main` обновляет `:main` и
`:sha-<полный SHA>`; pull request только собирает и проверяет образ.
Для ручной пересборки используйте Actions → Build and Publish Docker Image →
Run workflow → main. Сборка ветки вручную публикует SHA-тег, но не меняет `:main`.

Версия образа хранится в `karvpn-image-release.json`: upstream `5.0.0` и
`fork_revision: 1` дают `5.0.0-karvpn.1`. Для следующей нашей сборки увеличивается
`fork_revision`; при обновлении upstream меняется `upstream_version` и ревизия
начинается с 1. Workflow сверяет upstream с `pyproject.toml` и отказывается
публиковать релизный тег, не совпадающий с `v<версия>`. После слияния изменений
тег `v5.0.0-karvpn.1` запускает публикацию одноимённого Docker-тега. Python-версия
проекта и встроенная проверка upstream-обновлений сохраняют `5.0.0`;
`VERSION` и OCI label образа содержат полную версию KarVPN.

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
