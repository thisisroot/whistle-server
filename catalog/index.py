"""The published catalog index, and importing hand-maintained entries in the same format.

The index is the contract with the client (whistle-core's `index.rs`): every app
with at least one way to get it, its sources, and the current revision of each
live recipe.
"""

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from .models import App, Recipe, Source

FORMAT = 1
APP_FIELDS = ["name", "summary", "categories", "icon_url", "screenshots", "homepage", "license", "popularity"]
SOURCE_FIELDS = ["platforms", "arches", "version", "url", "sha256"]


def build_index(exclude_statuses=(), exclude_kinds=()):
    """The index clients download. `exclude_statuses` and `exclude_kinds`
    leave out recipes and sources, for clients too old to use them; apps left
    with nothing are left out too."""
    live_recipes = (
        Recipe.objects.exclude(status=Recipe.Status.RETIRED)
        .exclude(status__in=exclude_statuses)
        .filter(current_revision__isnull=False)
        .select_related("current_revision")
        .order_by("id")
    )
    apps = App.objects.order_by("id").prefetch_related(
        Prefetch("sources", queryset=Source.objects.exclude(kind__in=exclude_kinds).order_by("kind", "ref")),
        Prefetch("recipes", queryset=live_recipes),
    )
    out = []
    for app in apps:
        sources = [
            {"kind": s.kind, "ref": s.ref, **{f: getattr(s, f) for f in SOURCE_FIELDS}}
            for s in app.sources.all()
        ]
        recipes = [
            {
                "id": r.id,
                "runtime": r.runtime,
                "status": r.status,
                "revision": r.current_revision.number,
                "spec": r.current_revision.spec,
            }
            for r in app.recipes.all()
        ]
        if not sources and not recipes:
            continue
        out.append({"id": app.id, **{f: getattr(app, f) for f in APP_FIELDS}, "sources": sources, "recipes": recipes})
    return {"format": FORMAT, "generated_at": timezone.now().isoformat(timespec="seconds"), "apps": out}


@transaction.atomic
def import_index(data):
    """Create or update apps and sources from an index-format file (origin "manual").

    Recipes in the file are ignored; they come from the whistle-recipes repo.
    Returns (apps, sources) counts.
    """
    if data.get("format") != FORMAT:
        raise ValueError(f"expected index format {FORMAT}, got {data.get('format')!r}")
    n_apps = n_sources = 0
    for entry in data["apps"]:
        app = App.objects.filter(pk=entry["id"]).first() or App(id=entry["id"])
        for field in APP_FIELDS:
            if field in entry:
                setattr(app, field, entry[field])
        app.full_clean()
        app.save()
        n_apps += 1
        for s in entry.get("sources", []):
            source = Source.objects.filter(app=app, kind=s["kind"], ref=s["ref"]).first()
            source = source or Source(app=app, kind=s["kind"], ref=s["ref"])
            for field in SOURCE_FIELDS:
                setattr(source, field, s.get(field, [] if field == "arches" else ""))
            source.origin = Source.Origin.MANUAL
            source.full_clean()
            source.save()
            n_sources += 1
    return n_apps, n_sources
