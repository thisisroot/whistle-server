"""Native package names from the distros' own AppStream metadata.

Distros publish AppStream catalogs that map each app's component ID (the same
reverse-DNS ID Flathub uses, e.g. org.gimp.GIMP) to the package that ships it.
Matching on that ID, never on names, is what keeps Whistle from installing a
different package that merely has the same name.
"""

import gzip
import io
import lzma
import re
import shutil
import struct
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import urljoin

import yaml
from dataclasses import dataclass
from typing import Callable, Iterable, NamedTuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import App, Source

USER_AGENT = "whistle-server (+https://github.com/thisisroot/whistle-server)"
FEDORA_MIRROR = "https://mirrors.kernel.org/fedora"
# Same rule the client applies before passing a name to a package manager.
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/@-]*$")

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
# What older AppStream files (Fedora's) call a desktop application.
TYPE_ALIASES = {"desktop": "desktop-application"}


class Component(NamedTuple):
    id: str
    package: str
    desktop_ids: list[str]
    type: str = "desktop-application"
    name: str = ""
    summary: str = ""
    categories: list[str] = []
    license: str = ""
    homepage: str = ""
    icon: str = ""
    screenshots: list[str] = []


Fetch = Callable[[str], bytes]
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# Screenshots are stored at about this width: sharp on a store page, not the
# multi-megabyte originals.
SCREENSHOT_WIDTH = 1248
MAX_SCREENSHOTS = 5


def _https(url):
    return url if url.startswith("https://") and len(url) <= 500 else ""


def _best_image(images):
    """From (width, url) pairs, the one nearest SCREENSHOT_WIDTH, preferring
    smaller over larger."""
    images = [(w, u) for w, u in images if u]
    if not images:
        return ""
    return min(images, key=lambda wu: (wu[0] > SCREENSHOT_WIDTH, abs(SCREENSHOT_WIDTH - wu[0])))[1]


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def _text(fetch, url):
    return fetch(url).decode("utf-8", "replace")


def _decompress(raw):
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(raw)
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        return _unzstd(raw)
    return raw


def _unzstd(raw):
    """Fedora's RPMs are zstd-compressed. Use the zstandard package if it's
    installed, otherwise the zstd command (GitHub's Ubuntu runners have it)."""
    try:
        import zstandard
    except ImportError:
        if not shutil.which("zstd"):
            raise ValueError("zstd data: install the zstandard package or the zstd command") from None
        return subprocess.run(["zstd", "-dc"], input=raw, capture_output=True, check=True).stdout
    return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()


# --- Parsers -----------------------------------------------------------------


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _dep11_media(doc, media_base):
    """Icon and screenshot URLs from one DEP-11 desktop-application document."""
    try:
        data = yaml.load(doc, Loader=YAML_LOADER) or {}
    except yaml.YAMLError:
        return "", []

    def absolute(url):
        return _https(urljoin(media_base, url)) if url else ""

    remote = (data.get("Icon") or {}).get("remote") or []
    icon = max(remote, key=lambda i: i.get("width") or 0, default={}).get("url", "")
    shots = []
    for shot in sorted(data.get("Screenshots") or [], key=lambda s: not s.get("default")):
        images = [(t.get("width") or 0, absolute(t.get("url", ""))) for t in shot.get("thumbnails") or []]
        source = shot.get("source-image") or {}
        images.append((source.get("width") or 0, absolute(source.get("url", ""))))
        if best := _best_image(images):
            shots.append(best)
    return absolute(icon), shots[:MAX_SCREENSHOTS]


def parse_dep11(raw) -> Iterable[Component]:
    """Debian/Ubuntu DEP-11 YAML. Scanned line by line, which is much faster
    than a YAML parser on these large files: every field we need is a
    top-level key, a list under one, or its untranslated (`C`) value. Only
    desktop apps' icons and screenshots, which are nested deeper, go through
    the YAML parser."""
    text = _decompress(raw).decode("utf-8", "replace")
    docs = text.split("\n---\n")
    header = re.search(r"^MediaBaseUrl:\s*(\S+)", docs[0] + "\n" + (docs[1] if len(docs) > 1 else ""), re.M)
    media_base = header.group(1).rstrip("/") + "/" if header else ""
    for doc in docs:
        top = {}
        lists = defaultdict(list)
        nested = {}
        section = None
        for line in doc.splitlines():
            if not line or line.startswith("#"):
                continue
            if not line[0].isspace() and not line.startswith("-"):
                key, _, value = line.partition(":")
                section = key
                if value.strip():
                    top[key] = _scalar(value)
            elif section:
                item = line.strip()
                if item.startswith("- "):
                    lists[section].append(_scalar(item[2:]))
                elif line.startswith("  ") and not line.startswith("   ") and ":" in item:
                    key, _, value = item.partition(":")
                    nested.setdefault((section, key.strip()), _scalar(value))
        cid, package = top.get("ID"), top.get("Package")
        if cid and package:
            kind = top.get("Type", "")
            icon, screenshots = _dep11_media(doc, media_base) if kind == "desktop-application" else ("", [])
            yield Component(
                id=cid,
                package=package,
                desktop_ids=lists["Launchable"],
                type=top.get("Type", ""),
                name=nested.get(("Name", "C"), ""),
                summary=nested.get(("Summary", "C"), ""),
                categories=lists["Categories"],
                license=top.get("ProjectLicense", ""),
                homepage=nested.get(("Url", "homepage"), ""),
                icon=icon,
                screenshots=screenshots,
            )


def parse_appstream_xml(raw) -> Iterable[Component]:
    """AppStream collection XML (Arch, Fedora, openSUSE): the components that
    name their package."""
    return (c for c in xml_components(raw) if c.package)


def xml_components(raw) -> Iterable[Component]:
    """Every component in an AppStream collection XML file, with or without a
    package (Flathub's has none)."""
    def untranslated(element, tag):
        for child in element.findall(tag):
            if child.get(XML_LANG) is None:
                return (child.text or "").strip()
        return ""

    for _, element in ET.iterparse(io.BytesIO(_decompress(raw))):
        if element.tag != "component":
            continue
        cid = (element.findtext("id") or "").strip()
        package = (element.findtext("pkgname") or "").strip()
        desktop_ids = [
            (lau.text or "").strip() for lau in element.findall("launchable") if lau.get("type") == "desktop-id"
        ]
        if cid:
            kind = element.get("type", "")
            icons = [
                (int(i.get("width") or 0), _https((i.text or "").strip()))
                for i in element.findall("icon")
                if i.get("type") == "remote"
            ]
            shots = []
            for shot in sorted(element.findall("screenshots/screenshot"), key=lambda s: s.get("type") != "default"):
                images = [
                    (int(img.get("width") or 0), _https((img.text or "").strip()))
                    for img in shot.findall("image")
                ]
                if best := _best_image(images):
                    shots.append(best)
            yield Component(
                id=cid,
                package=package,
                desktop_ids=desktop_ids,
                type=TYPE_ALIASES.get(kind, kind),
                name=untranslated(element, "name"),
                summary=untranslated(element, "summary"),
                categories=[(c.text or "").strip() for c in element.findall("categories/category")],
                license=(element.findtext("project_license") or "").strip(),
                homepage=next(
                    ((u.text or "").strip() for u in element.findall("url") if u.get("type") == "homepage"), ""
                ),
                icon=max(icons, default=(0, ""))[1],
                screenshots=shots[:MAX_SCREENSHOTS],
            )
        element.clear()


def rpm_files(raw):
    """Yield (path, data) for every regular file in an RPM package."""
    if raw[:4] != b"\xed\xab\xee\xdb":
        raise ValueError("not an RPM file")

    def skip_header(pos):
        if raw[pos : pos + 3] != b"\x8e\xad\xe8":
            raise ValueError("bad RPM header")
        count, size = struct.unpack(">II", raw[pos + 8 : pos + 16])
        return pos + 16 + count * 16 + size

    pos = skip_header(96)  # the signature header, after the 96-byte lead
    pos += -pos % 8  # which is padded to 8 bytes
    pos = skip_header(pos)
    yield from _cpio(_decompress(raw[pos:]))


def _cpio(data):
    pos = 0
    while pos + 110 <= len(data):
        if data[pos : pos + 6] not in (b"070701", b"070702"):
            raise ValueError("unsupported cpio format")
        fields = [int(data[pos + 6 + i * 8 : pos + 14 + i * 8], 16) for i in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name = data[pos + 110 : pos + 110 + name_size - 1].decode("utf-8", "replace")
        pos += 110 + name_size
        pos += -pos % 4
        if name == "TRAILER!!!":
            return
        body = data[pos : pos + size]
        pos += size
        pos += -pos % 4
        if mode & 0o170000 == 0o100000:
            yield name, body


def parse_appstream_rpm(raw) -> Iterable[Component]:
    """Fedora ships its AppStream catalog as the appstream-data package."""
    found = False
    for path, body in rpm_files(raw):
        if "/swcatalog/xml/" in path or "/app-info/xmls/" in path:
            if path.endswith((".xml", ".xml.gz", ".xml.xz")):
                found = True
                yield from parse_appstream_xml(body)
    if not found:
        raise ValueError("no AppStream catalog in the RPM")


# --- Feeds -------------------------------------------------------------------


def debian_urls(fetch):
    return [
        f"https://deb.debian.org/debian/dists/stable/{c}/dep11/Components-amd64.yml.gz"
        for c in ("main", "contrib", "non-free")
    ]


def ubuntu_urls(fetch):
    """The newest supported LTS."""
    meta = _text(fetch, "https://changelogs.ubuntu.com/meta-release-lts")
    supported = [block for block in meta.split("\n\n") if "Supported: 1" in block]
    match = re.search(r"^Dist: (\S+)", supported[-1] if supported else "", re.M)
    if not match:
        raise ValueError("can't find the current Ubuntu LTS")
    dist = match.group(1)
    return [
        f"https://archive.ubuntu.com/ubuntu/dists/{dist}/{c}/dep11/Components-amd64.yml.gz"
        for c in ("main", "universe", "multiverse")
    ]


def arch_urls(fetch):
    base = "https://sources.archlinux.org/other/packages/archlinux-appstream-data/"
    versions = re.findall(r'href="(\d{8})/"', _text(fetch, base))
    if not versions:
        raise ValueError("can't find archlinux-appstream-data")
    # core has no desktop apps; multilib has Steam.
    return [f"{base}{max(versions)}/{repo}/Components-x86_64.xml.gz" for repo in ("extra", "multilib")]


def opensuse_urls(fetch):
    urls = []
    for repo in ("oss", "non-oss"):
        base = f"https://download.opensuse.org/tumbleweed/repo/{repo}/"
        repomd = ET.fromstring(fetch(base + "repodata/repomd.xml"))
        ns = {"r": "http://linux.duke.edu/metadata/repo"}
        location = repomd.find("r:data[@type='appdata']/r:location", ns)
        if location is None:
            raise ValueError(f"no appdata in the openSUSE {repo} repo")
        urls.append(base + location.get("href"))
    return urls


def fedora_urls(fetch):
    """The appstream-data RPM of the newest Fedora release."""
    releases = sorted({int(v) for v in re.findall(r'href="(\d+)/"', _text(fetch, f"{FEDORA_MIRROR}/releases/"))})
    for version in reversed(releases[-3:]):
        folder = f"{FEDORA_MIRROR}/releases/{version}/Everything/x86_64/os/Packages/a/"
        try:
            listing = _text(fetch, folder)
        except OSError:
            continue
        rpms = sorted(set(re.findall(r'href="(appstream-data-[^"]+\.noarch\.rpm)"', listing)))
        if rpms:
            return [folder + rpms[-1]]
    raise ValueError("can't find Fedora's appstream-data package")


@dataclass(frozen=True)
class Feed:
    name: str
    kind: str
    platform: str
    urls: Callable[[Fetch], list[str]]
    parse: Callable[[bytes], Iterable[Component]]


FEEDS = [
    Feed("debian", Source.Kind.APT, "linux:debian", debian_urls, parse_dep11),
    Feed("ubuntu", Source.Kind.APT, "linux:ubuntu", ubuntu_urls, parse_dep11),
    Feed("arch", Source.Kind.PACMAN, "linux:arch", arch_urls, parse_appstream_xml),
    Feed("fedora", Source.Kind.DNF, "linux:fedora", fedora_urls, parse_appstream_rpm),
    Feed("opensuse", Source.Kind.ZYPPER, "linux:opensuse", opensuse_urls, parse_appstream_xml),
]


# --- Matching ----------------------------------------------------------------


def _key(value):
    value = value.strip().lower()
    return value.removesuffix(".desktop")


def build_lookup(components: Iterable[Component]):
    """Component ID (and each desktop ID) -> packages that ship it."""
    lookup = defaultdict(set)
    for c in components:
        for key in {_key(c.id), *(_key(d) for d in c.desktop_ids)}:
            if key:
                lookup[key].add(c.package)
    return lookup


# Like the App model's ids: reverse-DNS, or a menu entry name ("3dchess.desktop").
REVERSE_DNS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+-]*(\.[A-Za-z0-9_+-]+)+$")
CATEGORY = re.compile(r"^[A-Za-z0-9-]+$")


def _component_keys(c):
    return {k for k in {_key(c.id), *(_key(d) for d in c.desktop_ids)} if k}


def _url_field(url):
    """A URL that fits the models' URL fields, or nothing."""
    return url if url.startswith(("https://", "http://")) and len(url) <= 200 else ""


def fill_media(components):
    """Give apps the icon, screenshots and homepage they lack, from the first
    component (in the order given) that has them. Never overwrites."""
    media = {}
    for c in components:
        if c.icon or c.screenshots or c.homepage:
            for key in _component_keys(c):
                media.setdefault(key, c)
    filled = 0
    for app in App.objects.filter(Q(icon_url="") | Q(screenshots=[]) | Q(homepage="")):
        c = media.get(_key(app.id)) or media.get(_key(app.appstream_id))
        if c is None:
            continue
        changed = False
        for field, value in (("icon_url", _url_field(c.icon)), ("homepage", _url_field(c.homepage))):
            if value and not getattr(app, field):
                setattr(app, field, value)
                changed = True
        if c.screenshots and not app.screenshots:
            app.screenshots = c.screenshots
            changed = True
        if changed:
            app.save(update_fields=["icon_url", "homepage", "screenshots"])
            filled += 1
    return filled


FLATHUB_APPSTREAM = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"


def ingest_flathub_media(fetch=fetch_bytes):
    """Screenshots, icons and homepages for Flathub apps, from Flathub's own
    AppStream file (one download for all apps). Returns how many apps gained some."""
    components = [c for c in xml_components(fetch(FLATHUB_APPSTREAM)) if c.type == "desktop-application"]
    with transaction.atomic():
        return fill_media(components)


def _create_distro_apps(candidates, existing_keys):
    """Create the desktop apps only distros ship (they aren't on Flathub or in
    the seed). The first feed that lists an app provides its name and summary.
    Returns how many were created."""
    created = 0
    for c in candidates:
        keys = _component_keys(c)
        # Old-style ids like "homebank.desktop" keep their suffix, and bare
        # ones like "qimgv" get it: a single word isn't a valid app id, and the
        # suffix is ignored when matching.
        app_id = c.id.removesuffix(".desktop")
        if not REVERSE_DNS.match(app_id):
            app_id = f"{app_id}.desktop"
        if keys & existing_keys or not REVERSE_DNS.match(app_id) or not c.name or not PACKAGE.match(c.package):
            continue
        App.objects.create(
            id=app_id,
            name=c.name[:255],
            summary=c.summary[:255],
            categories=[cat for cat in dict.fromkeys(c.categories) if CATEGORY.match(cat)],
            license=c.license[:255],
            homepage=_url_field(c.homepage),
            icon_url=_url_field(c.icon),
            screenshots=c.screenshots,
            appstream_id=app_id,
        )
        existing_keys |= keys
        created += 1
    return created


def ingest_appstream(feeds=FEEDS, fetch=fetch_bytes, log=lambda msg: None):
    """Add native sources for every app a distro's AppStream data knows, and
    add the desktop apps that only the distros ship.

    A feed that fails leaves that package kind's previous sources in place.
    Apps with a hand-written (or locked) source of the same kind are skipped.
    Returns {feed name: matched apps, or the error, "new apps": created}.
    """
    lookups = {}
    candidates = []  # desktop apps, in feed order
    results, failed_kinds = {}, set()
    for feed in feeds:
        try:
            components = []
            for url in feed.urls(fetch):
                log(f"{feed.name}: {url}")
                components.extend(feed.parse(fetch(url)))
        except Exception as exc:  # one broken mirror must not stop the others
            results[feed.name] = f"failed: {exc}"
            failed_kinds.add(feed.kind)
            continue
        lookups[feed] = build_lookup(components)
        candidates += [c for c in components if c.type == "desktop-application"]

    now = timezone.now()
    complete = {f.kind for f in feeds} - failed_kinds
    with transaction.atomic():
        existing_keys = set()
        for app_id, appstream_id in App.objects.values_list("id", "appstream_id"):
            existing_keys |= {_key(app_id), _key(appstream_id)} - {""}
        results["new apps"] = _create_distro_apps(candidates, existing_keys)
        results["media"] = fill_media(candidates)

        apps = list(App.objects.values_list("id", "appstream_id"))
        matches = defaultdict(set)  # (app id, kind, package) -> platforms
        for feed, lookup in lookups.items():
            matched = 0
            for app_id, appstream_id in apps:
                packages = set()
                for key in {app_id, appstream_id} - {""}:
                    packages |= lookup.get(_key(key), set())
                packages = {p for p in packages if PACKAGE.match(p)}
                if packages:
                    package = min(packages, key=lambda p: (len(p), p))
                    matches[(app_id, feed.kind, package)].add(feed.platform)
                    matched += 1
            results[feed.name] = matched

        Source.objects.filter(origin=Source.Origin.APPSTREAM, locked=False, kind__in=complete).delete()
        curated = set(
            Source.objects.filter(Q(origin=Source.Origin.MANUAL) | Q(locked=True)).values_list("app_id", "kind")
        )
        for (app_id, kind, package), platforms in matches.items():
            if (app_id, kind) in curated:
                continue
            source = Source.objects.filter(app_id=app_id, kind=kind, ref=package).first()
            if source is None:
                source = Source(app_id=app_id, kind=kind, ref=package, origin=Source.Origin.APPSTREAM, platforms=[])
            source.platforms = sorted(set(source.platforms) | platforms)
            source.last_seen_at = now
            source.save()
    return results
