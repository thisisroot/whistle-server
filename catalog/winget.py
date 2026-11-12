"""Windows apps from winget's package repository, as untested Wine recipes.

microsoft/winget-pkgs (MIT) describes each package's installer: the vendor's
URL, its SHA-256, the installer type and its silent-install switches. That is
most of a Wine recipe; the menu entries come from the Start Menu shortcuts the
installer creates. Nobody has tried these recipes, so they are published as
`untested`, and the client checks that the app starts after installing it.
"""

import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import yaml
from django.db import transaction
from django.utils import timezone

from .models import App, Recipe, Source
from .recipes import RecipeInvalid, index_recipe

REPO = "https://github.com/microsoft/winget-pkgs.git"
PATH_PREFIX = "winget-pkgs:"
REVERSE_DNS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Silent switches for installer types that have well-known ones.
DEFAULT_SILENT = {
    "nullsoft": ["/S"],
    "inno": ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
    "msi": ["/qn", "/norestart"],
    "wix": ["/qn", "/norestart"],
    "burn": ["/quiet", "/norestart"],
}
# Most likely to work under Wine first.
TYPE_ORDER = ["inno", "nullsoft", "msi", "wix", "burn", "exe", "portable"]
ARCH_ORDER = ["x64", "x86", "neutral"]
# winget dependencies that winetricks can provide.
WINETRICKS = {
    "Microsoft.VCRedist.2015+.x64": "vcrun2022",
    "Microsoft.VCRedist.2015+.x86": "vcrun2022",
    "Microsoft.DotNet.DesktopRuntime.6": "dotnetdesktop6",
    "Microsoft.DotNet.DesktopRuntime.7": "dotnetdesktop7",
    "Microsoft.DotNet.DesktopRuntime.8": "dotnetdesktop8",
}


# winget tags that mean a freedesktop main category.
TAG_CATEGORIES = {
    "game": "Game", "games": "Game", "gaming": "Game",
    "audio": "AudioVideo", "music": "AudioVideo", "video": "AudioVideo", "media": "AudioVideo",
    "media-player": "AudioVideo", "player": "AudioVideo", "podcast": "AudioVideo", "streaming": "AudioVideo",
    "development": "Development", "developer-tools": "Development", "programming": "Development",
    "ide": "Development", "code": "Development", "git": "Development", "debugger": "Development",
    "education": "Education", "learning": "Education",
    "graphics": "Graphics", "image": "Graphics", "photo": "Graphics", "drawing": "Graphics",
    "3d": "Graphics", "design": "Graphics", "image-editor": "Graphics",
    "browser": "Network", "web-browser": "Network", "email": "Network", "chat": "Network",
    "messaging": "Network", "ftp": "Network", "vpn": "Network", "network": "Network", "download": "Network",
    "office": "Office", "productivity": "Office", "pdf": "Office", "spreadsheet": "Office",
    "notes": "Office", "note-taking": "Office", "word-processor": "Office",
    "science": "Science", "math": "Science", "mathematics": "Science",
    "system": "System", "monitoring": "System", "backup": "System", "disk": "System", "driver": "System",
    "utility": "Utility", "utilities": "Utility", "tool": "Utility", "tools": "Utility",
    "archive": "Utility", "compression": "Utility", "text-editor": "Utility", "editor": "Utility",
}


# Hosts where many unrelated projects live: their site is the project's path.
SHARED_HOSTS = {"github.com", "gitlab.com", "codeberg.org", "sourceforge.net", "bitbucket.org", "gitee.com"}


def site(url):
    """What identifies a project's website: its host, or host and project path
    on shared hosts. Empty if there's no usable URL."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    if host in SHARED_HOSTS:
        parts = [p for p in parsed.path.lower().split("/") if p]
        if host == "sourceforge.net" and len(parts) >= 2 and parts[0] == "projects":
            return f"{host}/projects/{parts[1]}"
        return f"{host}/{'/'.join(parts[:2])}" if len(parts) >= 2 else ""
    return host


def categories_from_tags(tags):
    """Freedesktop main categories for a package's winget tags."""
    found = []
    for tag in tags or []:
        category = TAG_CATEGORIES.get(str(tag).strip().lower())
        if category and category not in found:
            found.append(category)
    return found


# --- Getting the manifests ---------------------------------------------------


def _version_key(version):
    """Sort winget versions: numbers numerically, other parts as text."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"[.\-+_ ]", version)]


def newest_version_dirs(paths):
    """From winget-pkgs file paths, the newest version folder of each package."""
    versions = {}
    for path in paths:
        if not path.endswith(".installer.yaml"):
            continue
        folder, name = path.rsplit("/", 1)
        package = name.removesuffix(".installer.yaml")
        version = folder.rsplit("/", 1)[1]
        best = versions.get(package)
        if best is None or _version_key(version) > _version_key(best[0]):
            versions[package] = (version, folder)
    return sorted(folder for _, folder in versions.values())


def checkout(repo_dir, log=lambda msg: None):
    """Clone (or update) winget-pkgs with only the newest version of each
    package checked out. Without history or old versions this is ~60k small
    files instead of ~800k. Returns the commit."""
    repo_dir = Path(repo_dir)

    def git(*args, **kwargs):
        return subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True, **kwargs)

    if (repo_dir / ".git").is_dir():
        log("updating winget-pkgs")
        git("fetch", "--depth", "1", "--filter=blob:none", "origin", "HEAD")
        git("reset", "--soft", "FETCH_HEAD")
    else:
        log("cloning winget-pkgs (file list only)")
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", REPO, str(repo_dir)],
            check=True,
            capture_output=True,
        )
    files = git("ls-tree", "-r", "--name-only", "HEAD", "manifests").stdout.splitlines()
    folders = newest_version_dirs(files)
    log(f"checking out the newest version of {len(folders)} packages")
    git("sparse-checkout", "init", "--cone")
    git("sparse-checkout", "set", "--cone", "--stdin", input="\n".join(folders))
    git("checkout", "--force", "HEAD")
    return git("rev-parse", "HEAD").stdout.strip()


# --- Turning a manifest into a recipe ----------------------------------------


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return yaml.load(f, Loader=LOADER) or {}


def _switches(text):
    if not text:
        return []
    try:
        return shlex.split(str(text))
    except ValueError:
        return str(text).split()


def pick_installer(manifest):
    """The installer most likely to work under Wine, with its type and the
    arguments that make it install silently. None if there is none."""
    top_type = (manifest.get("InstallerType") or "").lower()
    top_switches = manifest.get("InstallerSwitches") or {}
    top_deps = manifest.get("Dependencies") or {}
    options = []
    for installer in manifest.get("Installers") or []:
        kind = (installer.get("InstallerType") or top_type).lower()
        arch = installer.get("Architecture", "")
        url = installer.get("InstallerUrl") or ""
        sha256 = str(installer.get("InstallerSha256") or "")
        if kind not in TYPE_ORDER or arch not in ARCH_ORDER:
            continue
        if not url.startswith("https://") or len(url) > 2048 or not SHA256.match(sha256):
            continue
        switches = {**top_switches, **(installer.get("InstallerSwitches") or {})}
        args = _switches(switches.get("Silent")) or list(DEFAULT_SILENT.get(kind, []))
        if kind == "exe" and not args:
            continue  # an unknown installer that would wait for clicks
        args += _switches(switches.get("Custom"))
        deps = (installer.get("Dependencies") or top_deps).get("PackageDependencies") or []
        options.append({
            "type": kind,
            "arch": arch,
            "url": url,
            "sha256": sha256.lower(),
            "args": args,
            "dependencies": [d.get("PackageIdentifier", "") for d in deps if isinstance(d, dict)],
        })
    if not options:
        return None
    return min(options, key=lambda o: (ARCH_ORDER.index(o["arch"]), TYPE_ORDER.index(o["type"])))


def _app_id(package):
    if REVERSE_DNS.match(package):
        return package
    prefixed = f"winget.{package}"
    return prefixed if REVERSE_DNS.match(prefixed) else None


def make_recipe(package, app_id, name, version, installer):
    """The Wine recipe for one winget installer."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", package).strip("-")
    kind = installer["type"]
    extension = "msi" if kind in ("msi", "wix") else "exe"
    file_name = f"{slug}.{extension}" if kind == "portable" else f"{slug}-setup.{extension}"
    spec = {
        "schema_version": 1,
        "id": f"winget.{package}",
        "app": app_id,
        "runtime": "wine",
        "installer": {"type": "download", "url": installer["url"], "sha256": installer["sha256"], "file_name": file_name},
        "steps": [],
    }
    if version:
        spec["version_range"] = str(version)[:100]
    if installer["arch"] == "x64":
        spec["requires"] = {"arch": "win64"}
    verbs = sorted({WINETRICKS[d] for d in installer["dependencies"] if d in WINETRICKS})
    if verbs:
        spec["steps"].append({"winetricks": verbs})
    if kind == "portable":
        target = f"C:\\Program Files\\{slug}\\{file_name}"
        spec["steps"].append({"copy": {"from": file_name, "to": target}})
        spec["launchers"] = [{"name": name[:100] or package, "exe": target}]
    else:
        spec["steps"].append({"run_installer": {"args": installer["args"]}})
    return spec


def read_package(folder):
    """(package id, version, locale fields, installer manifest) for one version folder."""
    installer_file = next(folder.glob("*.installer.yaml"), None)
    if installer_file is None:
        return None
    package = installer_file.name.removesuffix(".installer.yaml")
    version_manifest = _load(folder / f"{package}.yaml")
    locale = version_manifest.get("DefaultLocale") or "en-US"
    locale_file = folder / f"{package}.locale.{locale}.yaml"
    details = _load(locale_file) if locale_file.is_file() else {}
    return package, version_manifest.get("PackageVersion") or folder.name, details, _load(installer_file)


# --- Import ------------------------------------------------------------------


def ingest_winget(manifests, commit, limit=None, log=lambda msg: None):
    """Import every winget package that has an installer Wine can run.
    Returns counts: {"apps": new apps, "recipes": recipes, "skipped": packages}."""
    manifests = Path(manifests)
    folders = sorted({f.parent for f in manifests.rglob("*.installer.yaml")})
    if limit:
        folders = folders[:limit]
    now = timezone.now()
    counts = {"apps": 0, "recipes": 0, "skipped": 0, "invalid": 0}
    seen_recipes = set()
    by_winget_id = dict(App.objects.exclude(winget_id="").values_list("winget_id", "id"))
    by_winget_id.update(
        Source.objects.filter(kind=Source.Kind.WINGET).values_list("ref", "app_id")
    )
    # The same app already in the catalog for Linux: same name and same website.
    twins = {}
    for app_id, name, homepage, winget_id in App.objects.exclude(homepage="").values_list(
        "id", "name", "homepage", "winget_id"
    ):
        if not winget_id and site(homepage):
            twins.setdefault((name.strip().lower(), site(homepage)), app_id)
    hand_written = set(
        Recipe.objects.filter(runtime=Recipe.Runtime.WINE).exclude(path__startswith=PATH_PREFIX).values_list("app_id", flat=True)
    )

    with transaction.atomic():
        for n, folder in enumerate(folders, 1):
            if n % 2000 == 0:
                log(f"{n}/{len(folders)} packages")
            try:
                parsed = read_package(folder)
            except (OSError, yaml.YAMLError):
                parsed = None
            if parsed is None:
                counts["skipped"] += 1
                continue
            package, version, details, manifest = parsed
            installer = pick_installer(manifest)
            homepage = details.get("PackageUrl") or details.get("PublisherUrl") or ""
            name = str(details.get("PackageName") or package).strip()
            # An app may have several winget packages (GIMP 2 and GIMP 3); all of them merge.
            twin = twins.get((name.lower(), site(homepage))) if site(homepage) else None
            app_id = by_winget_id.get(package) or twin or _app_id(package)
            if installer is None or app_id is None:
                counts["skipped"] += 1
                continue

            app = App.objects.filter(pk=app_id).first()
            if twin and app is not None:
                if not app.winget_id:
                    app.winget_id = package
                    app.save(update_fields=["winget_id"])
                counts["merged"] = counts.get("merged", 0) + 1
            if app is None:
                app = App.objects.create(
                    id=app_id,
                    name=name[:255],
                    summary=str(details.get("ShortDescription") or "").replace("\n", " ")[:255],
                    homepage=homepage if homepage.startswith("https://") and len(homepage) <= 200 else "",
                    license=str(details.get("License") or "")[:255],
                    categories=categories_from_tags(details.get("Tags")),
                    winget_id=package,
                )
                counts["apps"] += 1
            Source.objects.update_or_create(
                app=app,
                kind=Source.Kind.WINGET,
                ref=package,
                defaults={"platforms": ["windows"], "origin": Source.Origin.WINGET, "last_seen_at": now},
            )
            if app.pk in hand_written:
                continue  # a reviewed recipe beats a generated one

            spec = make_recipe(package, app.pk, app.name, version, installer)
            try:
                path = f"{PATH_PREFIX}{folder.relative_to(manifests.parent)}"
                revision = index_recipe(spec, path=path, git_commit=commit, from_file=False)
            except RecipeInvalid as exc:
                log(f"{package}: {exc}")
                counts["invalid"] += 1
                continue
            recipe = revision.recipe
            if recipe.status == Recipe.Status.DRAFT:
                recipe.status = Recipe.Status.UNTESTED
                recipe.save(update_fields=["status"])
            seen_recipes.add(recipe.pk)
            counts["recipes"] += 1

        if not limit:
            gone = Recipe.objects.filter(path__startswith=PATH_PREFIX).exclude(pk__in=seen_recipes)
            counts["retired"] = gone.exclude(status=Recipe.Status.RETIRED).update(status=Recipe.Status.RETIRED)
    return counts
