"""How widely each app is used, so search can put the apps people want first.

Two public sources: Flathub's install counts for the last month, and Debian's
popularity contest (popcon) for distro packages. Each is scaled 0 to 1000 on a
log scale against the most-installed app in that source, and an app gets the
higher of its two, so the sources' sizes don't have to match. A log scale
keeps a 3x lead visible at the top, where percentiles would flatten it.
"""

import gzip
import json
import math

from django.db import transaction

from .appstream import fetch_bytes
from .models import App, Source

FLATHUB_POPULAR = "https://flathub.org/api/v2/collection/popular?page={page}&per_page=250"
POPCON = "https://popcon.debian.org/by_inst.gz"
SCALE = 1000


def flathub_installs(fetch=fetch_bytes):
    """{Flathub app id: installs last month}."""
    installs, page = {}, 1
    while True:
        data = json.loads(fetch(FLATHUB_POPULAR.format(page=page)))
        for hit in data.get("hits", []):
            installs[hit["app_id"]] = int(hit.get("installs_last_month") or 0)
        if page >= int(data.get("totalPages") or 0):
            return installs
        page += 1


def popcon_installs(fetch=fetch_bytes):
    """{Debian package: systems that installed it}."""
    installs = {}
    for line in gzip.decompress(fetch(POPCON)).decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) >= 3 and not line.startswith("#") and fields[2].isdigit():
            installs[fields[1]] = int(fields[2])
    return installs


def scaled(counts):
    """{key: 0 to SCALE}, log(1 + count) against the largest count."""
    top = math.log1p(max(counts.values(), default=0))
    if not top:
        return dict.fromkeys(counts, 0)
    return {key: round(SCALE * math.log1p(count) / top) for key, count in counts.items()}


def ingest_popularity(fetch=fetch_bytes, log=lambda msg: None):
    """Set every app's popularity. A source that fails is skipped; with both
    failing, popularity stays as it was. Returns {source: apps it rated}."""
    results, scores = {}, {}
    try:
        flathub = flathub_installs(fetch)
        known = set(App.objects.filter(pk__in=flathub).values_list("pk", flat=True))
        rated = scaled({app: n for app, n in flathub.items() if app in known})
        for app, score in rated.items():
            scores[app] = max(scores.get(app, 0), score)
        results["flathub"] = len(rated)
    except (OSError, ValueError, KeyError) as e:
        results["flathub"] = f"failed: {e}"
    try:
        popcon = popcon_installs(fetch)
        # Scaled against the catalog's own packages, not Debian's base system,
        # which every machine has.
        by_package = {}
        for app, ref in Source.objects.filter(kind=Source.Kind.APT).values_list("app_id", "ref"):
            if ref in popcon:
                by_package.setdefault(ref, set()).add(app)
        rated = scaled({ref: popcon[ref] for ref in by_package})
        for ref, score in rated.items():
            for app in by_package[ref]:
                scores[app] = max(scores.get(app, 0), score)
        results["popcon"] = len(rated)
    except (OSError, ValueError) as e:
        results["popcon"] = f"failed: {e}"
    if not scores:
        return results
    with transaction.atomic():
        apps = list(App.objects.only("id", "popularity"))
        for app in apps:
            app.popularity = scores.get(app.id, 0)
        App.objects.bulk_update(apps, ["popularity"], batch_size=2000)
    log(f"rated {len(scores)} apps")
    return results
