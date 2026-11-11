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
from dataclasses import dataclass
from typing import Callable, Iterable

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import App, Source

USER_AGENT = "whistle-server (+https://github.com/thisisroot/whistle-server)"
FEDORA_MIRROR = "https://mirrors.kernel.org/fedora"
# Same rule the client applies before passing a name to a package manager.
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/@-]*$")

# (component id, package, desktop ids)
Component = tuple[str, str, list[str]]
Fetch = Callable[[str], bytes]


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


def parse_dep11(raw) -> Iterable[Component]:
    """Debian/Ubuntu DEP-11 YAML. Scanned line by line: the files are large and
    the fields we need are always top-level keys."""
    text = _decompress(raw).decode("utf-8", "replace")
    for doc in text.split("\n---\n"):
        cid = package = None
        desktop_ids = []
        in_launchable = False
        for line in doc.splitlines():
            if line.startswith("ID:"):
                cid = _scalar(line[3:])
            elif line.startswith("Package:"):
                package = _scalar(line[8:])
            elif line.startswith("Launchable:"):
                in_launchable = True
                continue
            elif line and not line[0].isspace() and not line.startswith("-"):
                in_launchable = False
            if in_launchable and line.lstrip().startswith("- "):
                desktop_ids.append(_scalar(line.lstrip()[2:]))
        if cid and package:
            yield cid, package, desktop_ids


def parse_appstream_xml(raw) -> Iterable[Component]:
    """AppStream collection XML (Arch, Fedora, openSUSE)."""
    for _, element in ET.iterparse(io.BytesIO(_decompress(raw))):
        if element.tag != "component":
            continue
        cid = (element.findtext("id") or "").strip()
        package = (element.findtext("pkgname") or "").strip()
        desktop_ids = [
            (lau.text or "").strip() for lau in element.findall("launchable") if lau.get("type") == "desktop-id"
        ]
        if cid and package:
            yield cid, package, desktop_ids
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
    return ["https://deb.debian.org/debian/dists/stable/main/dep11/Components-amd64.yml.gz"]


def ubuntu_urls(fetch):
    """The newest supported LTS."""
    meta = _text(fetch, "https://changelogs.ubuntu.com/meta-release-lts")
    supported = [block for block in meta.split("\n\n") if "Supported: 1" in block]
    match = re.search(r"^Dist: (\S+)", supported[-1] if supported else "", re.M)
    if not match:
        raise ValueError("can't find the current Ubuntu LTS")
    dist = match.group(1)
    return [f"https://archive.ubuntu.com/ubuntu/dists/{dist}/{c}/dep11/Components-amd64.yml.gz" for c in ("main", "universe")]


def arch_urls(fetch):
    base = "https://sources.archlinux.org/other/packages/archlinux-appstream-data/"
    versions = re.findall(r'href="(\d{8})/"', _text(fetch, base))
    if not versions:
        raise ValueError("can't find archlinux-appstream-data")
    return [f"{base}{max(versions)}/extra/Components-x86_64.xml.gz"]


def opensuse_urls(fetch):
    base = "https://download.opensuse.org/tumbleweed/repo/oss/"
    repomd = ET.fromstring(fetch(base + "repodata/repomd.xml"))
    ns = {"r": "http://linux.duke.edu/metadata/repo"}
    location = repomd.find("r:data[@type='appdata']/r:location", ns)
    if location is None:
        raise ValueError("no appdata in the openSUSE repo")
    return [base + location.get("href")]


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
    for cid, package, desktop_ids in components:
        for key in {_key(cid), *(_key(d) for d in desktop_ids)}:
            if key:
                lookup[key].add(package)
    return lookup


def ingest_appstream(feeds=FEEDS, fetch=fetch_bytes, log=lambda msg: None):
    """Add native sources for every app a distro's AppStream data knows.

    A feed that fails leaves that package kind's previous sources in place.
    Apps with a hand-written (or locked) source of the same kind are skipped.
    Returns {feed name: matched apps, or the error}.
    """
    apps = list(App.objects.values_list("id", "appstream_id"))
    matches = defaultdict(set)  # (app id, kind, package) -> platforms
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
        lookup = build_lookup(components)
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

    now = timezone.now()
    complete = {f.kind for f in feeds} - failed_kinds
    with transaction.atomic():
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
