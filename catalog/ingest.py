"""Fill the catalog from upstream app stores."""

import json
import re
import urllib.request

from django.db import transaction
from django.utils import timezone

from .models import App, Source

FLATHUB_POPULAR = "https://flathub.org/api/v2/collection/popular?page={page}&per_page={per_page}"
REVERSE_DNS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$")
CATEGORY = re.compile(r"^[A-Za-z0-9-]+$")

# Flathub lowercases freedesktop main categories.
MAIN_CATEGORIES = {
    c.lower(): c
    for c in ["AudioVideo", "Development", "Education", "Game", "Graphics", "Network", "Office", "Science", "System", "Utility"]
}


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "whistle-server"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ingest_flathub(limit=None, fetch=fetch_json):
    """Import Flathub's apps, most popular first: all of them, or the first
    `limit`. Returns (seen, skipped)."""
    now = timezone.now()
    per_page = min(limit or 250, 250)
    seen = skipped = 0
    page = 1
    while limit is None or seen + skipped < limit:
        data = fetch(FLATHUB_POPULAR.format(page=page, per_page=per_page))
        hits = data.get("hits") or []
        if limit is not None:
            hits = hits[: limit - seen - skipped]
        for hit in hits:
            if _upsert_flathub(hit, now):
                seen += 1
            else:
                skipped += 1
        if not hits or page >= data.get("totalPages", page):
            break
        page += 1
    return seen, skipped


@transaction.atomic
def _upsert_flathub(hit, now):
    app_id = hit.get("app_id") or ""
    if hit.get("type", "desktop-application") != "desktop-application" or not REVERSE_DNS.match(app_id):
        return False

    main = hit.get("main_categories")
    mains = [main] if isinstance(main, str) else list(main or [])
    categories = [MAIN_CATEGORIES[m] for m in mains if m in MAIN_CATEGORIES]
    categories += [c for c in hit.get("sub_categories") or [] if CATEGORY.match(c) and c not in categories]
    license = hit.get("project_license") or ""
    if license.startswith("LicenseRef-proprietary"):
        license = "LicenseRef-proprietary"
    icon = hit.get("icon") or ""
    fields = {
        "name": (hit.get("name") or app_id)[:255],
        "summary": (hit.get("summary") or "")[:255],
        "icon_url": icon if len(icon) <= 200 and icon.startswith("https://") else "",
        "license": license[:255],
        "categories": categories,
        "appstream_id": app_id,
    }

    app = App.objects.filter(pk=app_id).first()
    if app is None:
        app = App(id=app_id, **fields)
    else:
        # Curated apps keep their text; only fill what is missing.
        for field, value in fields.items():
            if not getattr(app, field):
                setattr(app, field, value)
    app.save()

    source = Source.objects.filter(app=app, kind=Source.Kind.FLATPAK, ref=app_id).first()
    if source and (source.locked or source.origin == Source.Origin.MANUAL):
        return True
    source = source or Source(app=app, kind=Source.Kind.FLATPAK, ref=app_id)
    source.platforms = ["linux"]
    source.origin = Source.Origin.FLATHUB
    source.last_seen_at = now
    source.save()
    return True
