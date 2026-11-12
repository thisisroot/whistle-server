# Contributing to whistle-server

The general guide, including the ground rules and commit sign-off, is in the
[whistle repo's CONTRIBUTING.md](https://github.com/thisisroot/whistle/blob/main/CONTRIBUTING.md).
This page covers what's specific to the catalog.

## Adding an app or fixing a package name

Edit [`seed/apps.json`](seed/apps.json). Hand-written entries always win over
imported data, so only add what Flathub and the distros' AppStream data get
wrong or don't have.

- Use the app's AppStream ID as `id` (the one Flathub uses, like `org.gimp.GIMP`).
- List a package under the most specific platform: `linux:debian` and
  `linux:ubuntu` separately if the names differ.
- Check the package exists in the distro you list. The client's
  `tests/distros/run.sh` checks the whole catalog against real containers.

## Changing a recipe's status

Edit [`seed/recipe-status.json`](seed/recipe-status.json). Only promote a recipe
to `verified` after installing, launching and removing the app yourself.

## Code changes

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python manage.py test
```

The tests read a whistle-recipes checkout next to this one, or the folder in
`WHISTLE_RECIPES_DIR`.

## License

By contributing, you agree that your contribution is licensed under AGPL-3.0-or-later.
