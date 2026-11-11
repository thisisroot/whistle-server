# whistle-server

The catalog behind [Whistle](https://github.com/thisisroot/whistle): which apps
exist, the ways to get each one, the recipes for running apps on the "other" OS,
and compatibility reports.

It builds `index.json`, the file the `whistle` client downloads with
`whistle update`.

## How the catalog is built

The published catalog is rebuilt from scratch every time, from:

1. `seed/apps.json`: hand-maintained apps and their native package names
   (apt, pacman, dnf, zypper, winget).
2. Flathub: the most popular apps and their Flatpak refs. Imported data only
   fills in what's missing, so it never overwrites hand-written entries.
3. [whistle-recipes](https://github.com/thisisroot/whistle-recipes): one recipe
   per app, with its review status taken from `seed/recipe-status.json`.

The [Publish index](.github/workflows/publish-index.yml) workflow does this on
every push to `main`, once a day and on demand. It runs the tests, builds the
catalog and uploads `index.json` to the
[`index` release](https://github.com/thisisroot/whistle-server/releases/tag/index).

To add or fix an app, edit `seed/apps.json`. To change a recipe's status, edit
`seed/recipe-status.json`. Edits made in the Django admin of a local database
are not published.

## Run it locally

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python manage.py migrate
uv run python manage.py import_index seed/apps.json
uv run python manage.py ingest_flathub --limit 1000
uv run python manage.py index_recipes ../whistle-recipes --status-file seed/recipe-status.json
uv run python manage.py build_index index.json
```

Then point the client at the result with `whistle update --from index.json`.

To browse the data, create an admin user (`uv run python manage.py createsuperuser`),
run `uv run python manage.py runserver` and open http://localhost:8000/admin/.

Run the tests with `uv run python manage.py test`. They read the sample recipes
from `../whistle-recipes`; set `WHISTLE_RECIPES_DIR` if your checkout is somewhere else.

## Commands

| Command                                         | What it does                                                |
| ----------------------------------------------- | ----------------------------------------------------------- |
| `import_index <file>`                           | Import apps and sources from an index-format JSON file.     |
| `ingest_flathub [--limit N]`                    | Import the N most popular Flathub apps (default 500).       |
| `index_recipes <repo> [--status-file F]`        | Index every recipe in a whistle-recipes checkout.           |
| `build_index <file>`                            | Write the index clients download.                           |

## Settings

| Variable                | Default                  | Effect                                        |
| ----------------------- | ------------------------ | --------------------------------------------- |
| `WHISTLE_DEBUG`         | `1`                      | Set to `0` in production.                     |
| `WHISTLE_SECRET_KEY`    | dev key when debugging   | Required when `WHISTLE_DEBUG=0`.              |
| `WHISTLE_ALLOWED_HOSTS` | `localhost,127.0.0.1`    | Comma-separated.                              |
| `WHISTLE_SQLITE_PATH`   | `db.sqlite3`             | SQLite file to use.                           |
| `POSTGRES_DB` and co.   | unset                    | Use Postgres instead of SQLite.               |
| `WHISTLE_RECIPES_DIR`   | `../whistle-recipes`     | Recipes checkout the tests read.              |

## Layout

- `catalog/models.py`: App, Source, Recipe, RecipeRevision, CompatReport.
- `catalog/index.py`: build the index, and import index-format seed files.
- `catalog/ingest.py`: the Flathub import.
- `catalog/recipes.py`: validate a recipe and store it as a revision.
- `accounts/`: users, for contributors and compat reports. Installing apps never needs an account.

## License

Apache-2.0.
