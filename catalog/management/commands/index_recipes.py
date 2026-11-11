import json
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from whistle_recipes.validate import load

from catalog.models import Recipe
from catalog.recipes import RecipeInvalid, index_recipe


class Command(BaseCommand):
    help = "Index every recipe in a whistle-recipes checkout."

    def add_arguments(self, parser):
        parser.add_argument("repo", type=Path, help="path to the whistle-recipes checkout")
        parser.add_argument("--commit", help="git commit to record (default: the checkout's HEAD)")
        parser.add_argument(
            "--status-file", type=Path,
            help="JSON {recipe_id: status} applied to the indexed recipes",
        )

    def handle(self, repo, commit, status_file, **options):
        if commit is None:
            try:
                commit = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise CommandError(f"{repo} is not a git checkout; pass --commit") from exc
        statuses = json.loads(status_file.read_text()) if status_file else {}

        failed = 0
        files = sorted((repo / "recipes").glob("*.y*ml"))
        for path in files:
            rel = path.relative_to(repo).as_posix()
            try:
                revision = index_recipe(load(path), path=rel, git_commit=commit)
            except RecipeInvalid as exc:
                failed += 1
                self.stderr.write(f"{rel}: {exc}")
                continue
            recipe = revision.recipe
            status = statuses.get(recipe.id)
            if status and status != recipe.status:
                if status not in Recipe.Status.values:
                    raise CommandError(f"{recipe.id}: unknown status {status!r}")
                recipe.status = status
                recipe.save(update_fields=["status", "updated_at"])
            self.stdout.write(f"{rel}: rev {revision.number} ({recipe.status})")
        if failed:
            raise CommandError(f"{failed} of {len(files)} recipes failed")
