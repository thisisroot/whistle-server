<p align="center">
  <img src="assets/logo.svg" width="96" alt="Whistle logo">
</p>

<h1 align="center">whistle-server</h1>

<p align="center">
  <b>The catalog behind <a href="https://github.com/thisisroot/whistle">Whistle</a>.</b><br>
  Which apps exist, every way to install each one, and the recipes for running apps on the other OS.
</p>

<p align="center">
  <a href="https://github.com/thisisroot/whistle-server/actions/workflows/publish-index.yml"><img src="https://img.shields.io/github/actions/workflow/status/thisisroot/whistle-server/publish-index.yml?branch=main&style=flat-square&label=publish%20index" alt="Publish index"></a>
  <a href="https://github.com/thisisroot/whistle-server/releases/tag/index"><img src="https://img.shields.io/github/release-date-pre/thisisroot/whistle-server?style=flat-square&label=catalog%20updated&color=0F1115" alt="Catalog updated"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-0F1115?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/django-5.2%2B-0F1115?style=flat-square&logo=django" alt="Django 5.2+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-0F1115?style=flat-square" alt="License: AGPL-3.0-or-later"></a>
</p>

<p align="center">
  <a href="#how-the-catalog-is-built">How it's built</a> ·
  <a href="#contributing-to-the-catalog">Contributing</a> ·
  <a href="#run-it-locally">Run it locally</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#settings">Settings</a>
</p>

The server builds **`catalog.json`**, the catalog the `whistle` client
downloads with `whistle update`. The current one is always at
[`releases/download/index/catalog.json`](https://github.com/thisisroot/whistle-server/releases/download/index/catalog.json)
(clients fetch the `.gz`, 2.7 MB).

| | |
| --- | --- |
| **Apps** | about 17,700 |
| **Linux apps** | about 5,800: all of Flathub, plus about 2,400 that only the distros ship |
| **With a native package** | 900 to 2,150 per distro (Debian, Ubuntu, Arch, Fedora, openSUSE) |
| **Windows apps** | about 12,400, from winget, as generated recipes, plus the reviewed ones in [whistle-recipes](https://github.com/thisisroot/whistle-recipes) |
| **Icons and screenshots** | about three quarters of the Linux apps |
| **Rebuilt** | on every push to `main` and once a day |

## How the catalog is built

```mermaid
flowchart LR
    S["seed/apps.json<br/>hand-maintained"] --> DB[("Catalog DB")]
    F["Flathub<br/>all apps"] --> DB
    A["Distro AppStream data<br/>Debian · Ubuntu · Arch<br/>Fedora · openSUSE"] --> DB
    R["whistle-recipes"] --> DB
    W["winget-pkgs<br/>Windows apps"] --> DB
    P["Flathub installs<br/>Debian popcon"] --> DB
    DB --> I["catalog.json"]
    I --> G["GitHub release 'index'"]
    G --> C["whistle update"]
```

The catalog is rebuilt from scratch every time, from these sources:

1. **`seed/apps.json`**: hand-maintained apps and their package names (apt,
   pacman, dnf, zypper, winget). Always wins over imported data.
2. **Flathub**: every app, with its name, icon, summary, homepage, screenshots
   and Flatpak ref. It only fills in what's missing.
3. **The distros' own AppStream metadata**: the package that ships each app on
   Debian (main, contrib, non-free), Ubuntu (main, universe, multiverse), Arch
   (extra, multilib), Fedora and openSUSE (oss, non-oss), plus the desktop apps
   that only the distros ship, with their icons and screenshots. Apps are matched by their
   AppStream ID (like `org.gimp.GIMP`), never by name, so a different program
   that happens to share a name is never picked. If a distro's feed is down, that
   distro keeps its previous data.
4. **[whistle-recipes](https://github.com/thisisroot/whistle-recipes)**: one
   reviewed recipe per app, with its status from `seed/recipe-status.json`.
5. **[winget-pkgs](https://github.com/microsoft/winget-pkgs)** (MIT): every
   Windows package with an installer Wine can run (Inno Setup, NSIS, MSI, Burn,
   portable, or an `.exe` with known silent switches) becomes an `untested`
   recipe. Only the newest version of each package is downloaded, without
   history: about 60,000 small files, in under a minute. A Windows app joins the
   Linux entry of the same app when both the name and the website match, and a
   reviewed recipe always wins over a generated one.
6. **Popularity**, so search can put the apps people want first: Flathub's
   installs over the last month, and Debian's
   [popularity contest](https://popcon.debian.org) for distro packages. Each is
   scaled from 0 to 1000 on a log scale, and an app gets the higher of the two.

The [Publish index](.github/workflows/publish-index.yml) workflow runs the
tests, builds the catalog, signs it and uploads it to the
[`index` release](https://github.com/thisisroot/whistle-server/releases/tag/index):

| File | For |
| --- | --- |
| `catalog.json`, `catalog.json.gz`, `catalog.json.minisig` | Whistle 0.2 and newer |
| `index.json`, `index.json.minisig` | Whistle 0.1, which rejects recipe statuses it doesn't know: no generated recipes and no Windows-only apps |

### Signing

Clients only accept a catalog signed with [minisign](https://jedisct1.github.io/minisign/)
by the key built into them. The public key is [`keys/index.pub`](keys/index.pub);
the workflow signs with the `MINISIGN_SECRET_KEY` repository secret and fails
if it isn't set. To check a catalog yourself:

```sh
minisign -V -p keys/index.pub -m catalog.json
```

To publish your own catalog, generate a key pair with `minisign -G`, sign with
it, and point clients at it with `WHISTLE_INDEX_URL` and `WHISTLE_INDEX_PUBKEY`.

## Contributing to the catalog

| To... | Edit |
| --- | --- |
| Add an app or fix a package name | [`seed/apps.json`](seed/apps.json) |
| Promote a recipe to `community` or `verified` | [`seed/recipe-status.json`](seed/recipe-status.json) |
| Add a recipe for running an app on the other OS | [whistle-recipes](https://github.com/thisisroot/whistle-recipes) |

Recipe statuses:

| Status | Meaning |
| --- | --- |
| `draft` | New and unreviewed. Clients only offer it with `--allow-draft`. |
| `community` | Reviewed and reported working. |
| `verified` | Tested end to end by a maintainer: install, launch, remove. |
| `retired` | Its file was removed from whistle-recipes. No longer offered; kept so old reports still make sense. |

Open a pull request. The catalog is code: edits made in the Django admin of a
local database are not published.

## Run it locally

You need [uv](https://docs.astral.sh/uv/), and a checkout of
[whistle-recipes](https://github.com/thisisroot/whistle-recipes) next to this one.

```sh
uv sync
uv run python manage.py migrate
uv run python manage.py import_index seed/apps.json
uv run python manage.py ingest_flathub
uv run python manage.py ingest_appstream
uv run python manage.py index_recipes ../whistle-recipes --status-file seed/recipe-status.json
uv run python manage.py ingest_winget ~/winget-pkgs     # cloned there on the first run
uv run python manage.py build_index catalog.json
```

Point the client at the result with `whistle update --from catalog.json`.

To browse the data, create an admin user with
`uv run python manage.py createsuperuser`, run `uv run python manage.py runserver`
and open http://localhost:8000/admin/.

Run the tests with `uv run python manage.py test`. Fedora's AppStream data is
zstd-compressed, so `ingest_appstream` needs either the `zstandard` Python
package or the `zstd` command.

## Commands

| Command | What it does |
| --- | --- |
| `import_index <file>` | Import apps and sources from an index-format JSON file. |
| `ingest_flathub [--limit N] [--no-media]` | Import all Flathub apps (or the N most popular), then their screenshots and homepages. |
| `ingest_appstream [--only FEED...] [--strict]` | Add native packages, distro-only apps, icons and screenshots from the distros' AppStream data. `--strict` fails if any feed fails. |
| `index_recipes <repo> [--status-file F]` | Index every recipe in a whistle-recipes checkout. |
| `ingest_winget <dir> [--no-update] [--limit N]` | Clone or update winget-pkgs in `<dir>`, and generate `untested` recipes for its Windows apps. |
| `ingest_popularity` | Rate every app from Flathub installs and Debian's popcon. |
| `build_index <file> [--exclude-status S] [--exclude-kind K]` | Write the index clients download; the exclusions make the one for older clients. |

## Settings

| Variable | Default | Effect |
| --- | --- | --- |
| `WHISTLE_DEBUG` | `1` | Set to `0` in production. |
| `WHISTLE_SECRET_KEY` | dev key when debugging | Required when `WHISTLE_DEBUG=0`. |
| `WHISTLE_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated. |
| `WHISTLE_SQLITE_PATH` | `db.sqlite3` | SQLite file to use. |
| `POSTGRES_DB` and co. | unset | Use Postgres instead of SQLite. |
| `WHISTLE_RECIPES_DIR` | `../whistle-recipes` | Recipes checkout the tests read. |

## Layout

| Path | Contents |
| --- | --- |
| [`catalog/models.py`](catalog/models.py) | App, Source, Recipe, RecipeRevision, CompatReport. |
| [`catalog/index.py`](catalog/index.py) | Build the index, and import index-format seed files. |
| [`catalog/ingest.py`](catalog/ingest.py) | The Flathub import. |
| [`catalog/appstream.py`](catalog/appstream.py) | The AppStream import: DEP-11, AppStream XML and Fedora's RPM; icons and screenshots. |
| [`catalog/winget.py`](catalog/winget.py) | The winget import: picking an installer and generating its recipe. |
| [`catalog/popularity.py`](catalog/popularity.py) | How widely each app is used. |
| [`catalog/recipes.py`](catalog/recipes.py) | Validate a recipe and store it as a revision. |
| [`accounts/`](accounts) | Users, for contributors and compatibility reports. Installing apps never needs an account. |
| [`seed/`](seed) | The hand-maintained part of the catalog. |

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version of this server for others, you must
share its source under the same license. The catalog data it publishes is not covered by this:
recipes are CC0, and imported metadata keeps its original terms.
