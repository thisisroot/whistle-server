"""Index recipe files from the whistle-recipes repo into the database."""

import hashlib
import json

from django.db import transaction
from django.db.models import Max
from whistle_recipes.validate import validate

from .models import App, Recipe, RecipeRevision


class RecipeInvalid(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def canonical_sha256(spec):
    data = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


@transaction.atomic
def index_recipe(spec, *, path, git_commit, from_file=True):
    """Validate a recipe and store it as a new revision.

    Idempotent: indexing an unchanged spec returns its existing revision (and
    makes it current again, which handles reverts). `from_file=False` is for
    generated recipes, whose id can't match a file name.
    """
    errors = validate(spec, path=path if from_file else None)
    if errors:
        raise RecipeInvalid(errors)
    try:
        app = App.objects.get(pk=spec["app"])
    except App.DoesNotExist:
        raise RecipeInvalid([f"app: unknown app {spec['app']!r}"]) from None

    recipe, created = Recipe.objects.select_for_update().get_or_create(
        pk=spec["id"],
        defaults={
            "app": app,
            "runtime": spec["runtime"],
            "version_range": spec.get("version_range", ""),
            "path": path,
        },
    )
    if not created:
        if recipe.app_id != app.pk:
            raise RecipeInvalid([f"app: recipe {recipe.pk} already belongs to {recipe.app_id}"])
        recipe.runtime = spec["runtime"]
        recipe.version_range = spec.get("version_range", "")
        recipe.path = path
        if recipe.status == Recipe.Status.RETIRED:
            recipe.status = Recipe.Status.DRAFT

    digest = canonical_sha256(spec)
    revision = recipe.revisions.filter(spec_sha256=digest).first()
    if revision is None:
        last = recipe.revisions.aggregate(n=Max("number"))["n"] or 0
        revision = RecipeRevision.objects.create(
            recipe=recipe, number=last + 1, git_commit=git_commit, spec=spec, spec_sha256=digest
        )
    recipe.current_revision = revision
    recipe.save()
    return revision
