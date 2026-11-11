from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from whistle_recipes.validate import load

from accounts.models import User

from .models import App, CompatReport, Recipe, Source
from .recipes import RecipeInvalid, index_recipe

RECIPES = Path(__file__).resolve().parents[2] / "whistle-recipes" / "recipes"
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
