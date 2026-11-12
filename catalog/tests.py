import json
import os
from io import StringIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from whistle_recipes.validate import load

from accounts.models import User

from .appstream import Feed, ingest_appstream, parse_appstream_rpm, parse_appstream_xml, parse_dep11
from .index import build_index, import_index
from .ingest import ingest_flathub
from .models import App, CompatReport, Recipe, Source
from .recipes import RecipeInvalid, index_recipe

# The whistle-recipes checkout; by default it sits next to this repo.
RECIPES_REPO = Path(os.environ.get("WHISTLE_RECIPES_DIR") or Path(__file__).resolve().parents[2] / "whistle-recipes")
RECIPES = RECIPES_REPO / "recipes"
SEED = Path(__file__).resolve().parents[1] / "seed"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class AppTests(TestCase):
    def test_id_must_be_reverse_dns(self):
        with self.assertRaises(ValidationError):
            App(id="firefox", name="Firefox").full_clean()
        App(id="org.mozilla.firefox", name="Firefox").full_clean()

    def test_blank_winget_ids_dont_collide(self):
        App.objects.create(id="org.a.A", name="A")
        App.objects.create(id="org.b.B", name="B")
        App.objects.create(id="org.c.C", name="C", winget_id="C.C")
        with self.assertRaises(IntegrityError):
            App.objects.create(id="org.d.D", name="D", winget_id="C.C")


class SourceTests(TestCase):
    def setUp(self):
        self.app = App.objects.create(id="org.mozilla.firefox", name="Firefox")

    def test_unknown_platform_rejected(self):
        source = Source(app=self.app, kind="apt", ref="firefox", platforms=["linux:gentoo"], origin="manual")
        with self.assertRaisesMessage(ValidationError, "linux:gentoo"):
            source.full_clean()

    def test_direct_download_needs_https_and_hash(self):
        with self.assertRaises(IntegrityError):
            Source.objects.create(
                app=self.app, kind="appimage", ref="ff", platforms=["linux"], origin="manual",
                url="http://example.org/ff.AppImage", sha256="a" * 64,
            )

    def test_direct_download_ok_when_pinned(self):
        Source.objects.create(
            app=self.app, kind="appimage", ref="ff", platforms=["linux"], origin="manual",
            url="https://example.org/ff.AppImage", sha256="a" * 64,
        )


class IndexRecipeTests(TestCase):
    def setUp(self):
        App.objects.create(id="org.notepad-plus-plus.NotepadPlusPlus", name="Notepad++")
        self.path = "recipes/org.notepad-plus-plus.NotepadPlusPlus.yaml"
        self.spec = load(RECIPES / "org.notepad-plus-plus.NotepadPlusPlus.yaml")

    def test_index_creates_revision(self):
        rev = index_recipe(self.spec, path=self.path, git_commit=COMMIT_A)
        recipe = Recipe.objects.get(pk=self.spec["id"])
        self.assertEqual(rev.number, 1)
        self.assertEqual(recipe.current_revision, rev)
        self.assertEqual(recipe.runtime, "wine")
        self.assertEqual(recipe.status, Recipe.Status.DRAFT)

    def test_reindex_unchanged_is_idempotent(self):
        first = index_recipe(self.spec, path=self.path, git_commit=COMMIT_A)
        again = index_recipe(dict(self.spec), path=self.path, git_commit=COMMIT_B)
        self.assertEqual(first, again)

    def test_change_then_revert(self):
        v1 = index_recipe(self.spec, path=self.path, git_commit=COMMIT_A)
        changed = {**self.spec, "version_range": "8.8"}
        v2 = index_recipe(changed, path=self.path, git_commit=COMMIT_B)
        self.assertEqual(v2.number, 2)
        reverted = index_recipe(self.spec, path=self.path, git_commit=COMMIT_B)
        self.assertEqual(reverted, v1)
        self.assertEqual(Recipe.objects.get().current_revision, v1)

    def test_invalid_recipe_rejected(self):
        bad = {**self.spec, "steps": [{"shell": "rm -rf ~"}]}
        with self.assertRaises(RecipeInvalid) as ctx:
            index_recipe(bad, path=self.path, git_commit=COMMIT_A)
        self.assertIn("'shell' is not one of", ctx.exception.errors[0])
        self.assertFalse(Recipe.objects.exists())

    def test_unknown_app_rejected(self):
        spec = load(RECIPES / "org.gnome.Builder.WSL.yaml")
        with self.assertRaisesMessage(RecipeInvalid, "unknown app"):
            index_recipe(spec, path="recipes/org.gnome.Builder.WSL.yaml", git_commit=COMMIT_A)


class CompatReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester")
        self.app = App.objects.create(id="org.notepad-plus-plus.NotepadPlusPlus", name="Notepad++")
        spec = load(RECIPES / "org.notepad-plus-plus.NotepadPlusPlus.yaml")
        self.rev = index_recipe(spec, path=f"recipes/{spec['id']}.yaml", git_commit=COMMIT_A)
        self.source = Source.objects.create(
            app=self.app, kind="winget", ref="Notepad++.Notepad++", platforms=["windows"], origin="winget"
        )

    def report(self, **kw):
        fields = dict(
            app=self.app, host_os="linux", distro="arch", runtime="wine",
            rating="gold", created_by=self.user, recipe_revision=self.rev,
        )
        fields.update(kw)
        return CompatReport(**fields)

    def test_valid_recipe_report(self):
        self.report().full_clean()

    def test_needs_source_or_recipe(self):
        with self.assertRaisesMessage(ValidationError, "exactly one"):
            self.report(recipe_revision=None).full_clean()

    def test_not_both_at_db_level(self):
        with self.assertRaises(IntegrityError):
            self.report(source=self.source).save()

    def test_runtime_must_match_recipe(self):
        with self.assertRaisesMessage(ValidationError, "Recipe runs on wine"):
            self.report(runtime="proton").full_clean()

    def test_linux_needs_distro(self):
        with self.assertRaisesMessage(ValidationError, "Required for Linux"):
            self.report(distro="").full_clean()

    def test_source_report_on_windows(self):
        self.report(recipe_revision=None, source=self.source, host_os="windows", distro="", runtime="native").full_clean()


class IndexTests(TestCase):
    def setUp(self):
        import_index(json.loads((SEED / "apps.json").read_text()))
        statuses = SEED / "recipe-status.json"
        call_command(
            "index_recipes", str(RECIPES_REPO), commit=COMMIT_A, status_file=statuses, stdout=StringIO()
        )

    def test_seed_imports_as_manual(self):
        firefox = App.objects.get(pk="org.mozilla.firefox")
        apt = {s.ref: s.platforms for s in firefox.sources.filter(kind="apt")}
        self.assertEqual(apt, {"firefox-esr": ["linux:debian"], "firefox": ["linux:ubuntu"]})
        self.assertTrue(all(s.origin == "manual" for s in Source.objects.all()))

    def test_reimport_is_idempotent(self):
        before = Source.objects.count()
        import_index(json.loads((SEED / "apps.json").read_text()))
        self.assertEqual(Source.objects.count(), before)

    def test_import_rejects_bad_ids(self):
        with self.assertRaises(ValidationError):
            import_index({"format": 1, "apps": [{"id": "firefox", "name": "Firefox"}]})

    def test_index_matches_the_client_contract(self):
        index = build_index()
        self.assertEqual(index["format"], 1)
        apps = {a["id"]: a for a in index["apps"]}
        npp = apps["org.notepad-plus-plus.NotepadPlusPlus"]
        self.assertEqual(npp["sources"][0], {
            "kind": "winget", "ref": "Notepad++.Notepad++", "platforms": ["windows"],
            "arches": [], "version": "", "url": "", "sha256": "",
        })
        recipe = npp["recipes"][0]
        self.assertEqual((recipe["runtime"], recipe["status"], recipe["revision"]), ("wine", "verified", 1))
        self.assertEqual(recipe["spec"]["id"], "org.notepad-plus-plus.NotepadPlusPlus")
        self.assertEqual(apps["com.microsoft.Office"]["recipes"][0]["status"], "draft")

    def test_index_leaves_out_retired_recipes_and_empty_apps(self):
        Recipe.objects.filter(pk="com.microsoft.Office2016").update(status=Recipe.Status.RETIRED)
        App.objects.create(id="org.example.Nothing", name="Nothing")
        ids = {a["id"] for a in build_index()["apps"]}
        self.assertNotIn("com.microsoft.Office", ids)
        self.assertNotIn("org.example.Nothing", ids)


class FlathubIngestTests(TestCase):
    def fake(self, hits, pages=1):
        calls = []

        def fetch(url):
            calls.append(url)
            return {"hits": hits, "totalPages": pages}

        return fetch, calls

    def test_creates_apps_and_flatpak_sources(self):
        fetch, _ = self.fake([
            {"app_id": "org.gimp.GIMP", "name": "GIMP", "summary": "Edit images", "type": "desktop-application",
             "main_categories": "graphics", "sub_categories": ["RasterGraphics"], "project_license": "GPL-3.0+",
             "icon": "https://dl.flathub.org/x.png"},
            {"app_id": "org.freedesktop.Platform", "type": "runtime"},
            {"app_id": "not-reverse-dns", "type": "desktop-application"},
        ])
        self.assertEqual(ingest_flathub(limit=10, fetch=fetch), (1, 2))
        app = App.objects.get(pk="org.gimp.GIMP")
        self.assertEqual(app.categories, ["Graphics", "RasterGraphics"])
        source = app.sources.get()
        self.assertEqual((source.kind, source.ref, source.origin), ("flatpak", "org.gimp.GIMP", "flathub"))
        self.assertIsNotNone(source.last_seen_at)

    def test_keeps_curated_text_and_manual_sources(self):
        app = App.objects.create(id="org.gimp.GIMP", name="GIMP", summary="Curated summary")
        Source.objects.create(app=app, kind="flatpak", ref="org.gimp.GIMP", platforms=["linux:arch"], origin="manual")
        fetch, _ = self.fake([{"app_id": "org.gimp.GIMP", "name": "GIMP 3", "summary": "Upstream summary",
                               "project_license": "LicenseRef-proprietary=https://x"}])
        ingest_flathub(limit=10, fetch=fetch)
        app.refresh_from_db()
        self.assertEqual((app.name, app.summary, app.license), ("GIMP", "Curated summary", "LicenseRef-proprietary"))
        self.assertEqual(app.sources.get().platforms, ["linux:arch"])

    def test_stops_at_limit_across_pages(self):
        hits = [{"app_id": f"org.example.App{i}", "name": f"App {i}"} for i in range(3)]
        fetch, calls = self.fake(hits, pages=5)
        self.assertEqual(ingest_flathub(limit=5, fetch=fetch), (5, 0))
        self.assertEqual(len(calls), 2)
        self.assertEqual(App.objects.count(), 3)


DEP11 = """File: DEP-11
Origin: debian-stable-main
---
Type: desktop-application
ID: org.gimp.GIMP
Package: gimp
Name:
  C: GNU Image Manipulation Program
Launchable:
  desktop-id:
  - gimp.desktop
Url:
  homepage: https://www.gimp.org/
---
Type: desktop-application
ID: 'org.example.Quoted.desktop'
Package: "quoted-app"
---
Type: addon
ID: org.example.NoPackage
"""

APPSTREAM_XML = """<?xml version="1.0"?>
<components version="1.0" origin="extra">
  <component type="desktop-application">
    <id>org.gimp.GIMP</id>
    <pkgname>gimp</pkgname>
    <launchable type="desktop-id">gimp.desktop</launchable>
  </component>
  <component type="desktop-application">
    <id>firefox.desktop</id>
    <pkgname>firefox</pkgname>
    <launchable type="desktop-id">org.mozilla.firefox.desktop</launchable>
  </component>
  <component type="desktop-application">
    <id>org.example.Bad</id>
    <pkgname>-oops</pkgname>
  </component>
</components>
"""


def gz(text):
    import gzip

    return gzip.compress(text.encode())


def fake_rpm(files):
    """A minimal RPM: lead, empty signature and main headers, gzip'd newc cpio."""
    import gzip
    import struct

    def header():
        return b"\x8e\xad\xe8\x01" + b"\0" * 4 + struct.pack(">II", 0, 0)

    def entry(name, body, mode):
        name_b = name.encode() + b"\0"
        fields = [0, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(name_b), 0]
        out = b"070701" + b"".join(b"%08X" % f for f in fields) + name_b
        out += b"\0" * (-len(out) % 4) + body
        return out + b"\0" * (-len(out) % 4)

    cpio = b"".join(entry(n, b, 0o100644) for n, b in files.items()) + entry("TRAILER!!!", b"", 0)
    lead = b"\xed\xab\xee\xdb" + b"\0" * 92
    sig = header()
    sig += b"\0" * (-(len(lead) + len(sig)) % 8)
    return lead + sig + header() + gzip.compress(cpio)


class AppStreamParserTests(TestCase):
    def test_dep11(self):
        self.assertEqual(list(parse_dep11(gz(DEP11))), [
            ("org.gimp.GIMP", "gimp", ["gimp.desktop"]),
            ("org.example.Quoted.desktop", "quoted-app", []),
        ])

    def test_xml(self):
        parsed = list(parse_appstream_xml(gz(APPSTREAM_XML)))
        self.assertEqual(parsed[0], ("org.gimp.GIMP", "gimp", ["gimp.desktop"]))
        self.assertEqual(parsed[1], ("firefox.desktop", "firefox", ["org.mozilla.firefox.desktop"]))

    def test_rpm(self):
        rpm = fake_rpm({
            "./usr/share/doc/README": b"hi",
            "./usr/share/swcatalog/xml/fedora.xml.gz": gz(APPSTREAM_XML),
        })
        self.assertEqual(len(list(parse_appstream_rpm(rpm))), 3)
        with self.assertRaisesMessage(ValueError, "no AppStream catalog"):
            list(parse_appstream_rpm(fake_rpm({"./usr/share/doc/README": b"hi"})))
        with self.assertRaisesMessage(ValueError, "not an RPM"):
            list(parse_appstream_rpm(b"nope"))


class AppStreamIngestTests(TestCase):
    def setUp(self):
        for app_id, name in [("org.gimp.GIMP", "GIMP"), ("org.mozilla.firefox", "Firefox"), ("org.example.Bad", "Bad")]:
            App.objects.create(id=app_id, name=name)
        self.files = {"https://x/debian": gz(DEP11), "https://x/arch": gz(APPSTREAM_XML)}

    def feeds(self, broken=()):
        def urls(name):
            return lambda fetch: [f"https://x/{name}"]

        return [
            Feed("debian", "apt", "linux:debian", urls("debian"), parse_dep11),
            Feed("ubuntu", "apt", "linux:ubuntu", urls("debian"), parse_dep11),
            Feed("arch", "pacman", "linux:arch", urls("broken" if "arch" in broken else "arch"), parse_appstream_xml),
        ]

    def fetch(self, url):
        if url not in self.files:
            raise OSError(f"404 {url}")
        return self.files[url]

    def test_matches_by_component_and_desktop_id(self):
        results = ingest_appstream(self.feeds(), fetch=self.fetch)
        self.assertEqual(results, {"debian": 1, "ubuntu": 1, "arch": 2})
        gimp_apt = Source.objects.get(app_id="org.gimp.GIMP", kind="apt")
        self.assertEqual((gimp_apt.ref, gimp_apt.platforms, gimp_apt.origin), ("gimp", ["linux:debian", "linux:ubuntu"], "appstream"))
        # Matched through the launchable desktop id.
        self.assertEqual(Source.objects.get(app_id="org.mozilla.firefox", kind="pacman").ref, "firefox")
        # Package names that could pass as options are dropped.
        self.assertFalse(Source.objects.filter(app_id="org.example.Bad").exists())

    def test_hand_written_sources_win(self):
        Source.objects.create(app_id="org.gimp.GIMP", kind="apt", ref="gimp-custom", platforms=["linux:debian"], origin="manual")
        ingest_appstream(self.feeds(), fetch=self.fetch)
        self.assertEqual(list(Source.objects.filter(app_id="org.gimp.GIMP", kind="apt").values_list("ref", flat=True)), ["gimp-custom"])

    def test_failed_feed_keeps_previous_sources(self):
        ingest_appstream(self.feeds(), fetch=self.fetch)
        results = ingest_appstream(self.feeds(broken=["arch"]), fetch=self.fetch)
        self.assertTrue(results["arch"].startswith("failed"))
        self.assertEqual(Source.objects.filter(kind="pacman").count(), 2)

    def test_rerun_drops_packages_that_disappeared(self):
        ingest_appstream(self.feeds(), fetch=self.fetch)
        self.files["https://x/arch"] = gz("<components></components>")
        ingest_appstream(self.feeds(), fetch=self.fetch)
        self.assertFalse(Source.objects.filter(kind="pacman").exists())
