from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

reverse_dns = RegexValidator(
    r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$",
    "Use a reverse-DNS id, e.g. org.gimp.GIMP.",
)
sha256_hex = RegexValidator(r"^[0-9a-f]{64}$", "Lowercase hex SHA-256.")
git_sha = RegexValidator(r"^[0-9a-f]{40}$", "Full 40-character git commit SHA.")

# Platform tags a Source can target. "linux" alone means any distro.
PLATFORMS = {
    "windows",
    "linux",
    "linux:arch",
    "linux:debian",
    "linux:ubuntu",
    "linux:fedora",
    "linux:opensuse",
}
ARCHES = {"x86_64", "aarch64"}


def validate_platforms(value):
    if not isinstance(value, list) or not value:
        raise ValidationError("List at least one platform.")
    unknown = sorted(set(value) - PLATFORMS)
    if unknown:
        raise ValidationError(f"Unknown platforms: {', '.join(unknown)}.")


def validate_arches(value):
    if not isinstance(value, list):
        raise ValidationError("Must be a list.")
    unknown = sorted(set(value) - ARCHES)
    if unknown:
        raise ValidationError(f"Unknown architectures: {', '.join(unknown)}.")


class App(models.Model):
    """One logical app, whatever OS it was built for."""

    id = models.CharField(primary_key=True, max_length=255, validators=[reverse_dns])
    name = models.CharField(max_length=255)
    summary = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    icon_url = models.URLField(blank=True)
    screenshots = models.JSONField(default=list, blank=True, help_text="Screenshot image URLs, best first.")
    homepage = models.URLField(blank=True)
    license = models.CharField(
        max_length=255, blank=True, help_text="SPDX expression, or LicenseRef-proprietary."
    )
    categories = models.JSONField(default=list, blank=True, help_text="freedesktop categories.")
    popularity = models.PositiveSmallIntegerField(
        default=0, help_text="0-1000: how widely it's used, from Flathub installs and Debian's popcon."
    )

    # Cross-references used by ingest to match the same app across sources.
    appstream_id = models.CharField(max_length=255, blank=True)
    repology_name = models.CharField(max_length=255, blank=True, db_index=True)
    winget_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["appstream_id"], condition=~Q(appstream_id=""), name="app_unique_appstream_id"
            ),
            models.UniqueConstraint(
                fields=["winget_id"], condition=~Q(winget_id=""), name="app_unique_winget_id"
            ),
        ]

    def __str__(self):
        return self.name


class Source(models.Model):
    """One way to obtain an app natively: a package, a Flatpak, an installer."""

    class Kind(models.TextChoices):
        PACMAN = "pacman"
        APT = "apt"
        DNF = "dnf"
        ZYPPER = "zypper"
        FLATPAK = "flatpak"
        APPIMAGE = "appimage"
        WINGET = "winget"
        VENDOR_INSTALLER = "vendor_installer"
        WSL = "wsl"

    class Origin(models.TextChoices):
        APPSTREAM = "appstream"
        FLATHUB = "flathub"
        WINGET = "winget"
        REPOLOGY = "repology"
        MANUAL = "manual"

    # Kinds Whistle downloads itself, so a URL and a pinned hash are mandatory.
    DIRECT_DOWNLOAD_KINDS = [Kind.APPIMAGE, Kind.VENDOR_INSTALLER]

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="sources")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    ref = models.CharField(max_length=512, help_text="Package name, Flatpak ref, winget id or URL.")
    platforms = models.JSONField(validators=[validate_platforms])
    arches = models.JSONField(
        default=list, blank=True, validators=[validate_arches], help_text="Empty means any."
    )
    version = models.CharField(max_length=100, blank=True)
    url = models.URLField(max_length=2048, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, validators=[sha256_hex])

    origin = models.CharField(max_length=32, choices=Origin.choices)
    locked = models.BooleanField(
        default=False, help_text="Manual override: ingest jobs never modify or prune this row."
    )
    last_seen_at = models.DateTimeField(
        null=True, blank=True, help_text="Last time an ingest job saw this source upstream."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app", "kind", "ref"], name="source_unique_app_kind_ref"),
            models.CheckConstraint(
                condition=~Q(kind__in=["appimage", "vendor_installer"])
                | (Q(url__startswith="https://") & ~Q(sha256="")),
                name="source_direct_download_pinned",
            ),
        ]
        indexes = [models.Index(fields=["kind", "ref"])]

    def __str__(self):
        return f"{self.app_id} via {self.kind}:{self.ref}"


class Recipe(models.Model):
    """How to run an app on a host it wasn't built for. Mirrors one file in whistle-recipes."""

    class Runtime(models.TextChoices):
        WINE = "wine"
        PROTON = "proton"
        VM = "vm"
        WSL = "wsl"

    class Status(models.TextChoices):
        DRAFT = "draft"
        UNTESTED = "untested", "Untested (generated, offered with a warning)"
        COMMUNITY = "community"
        VERIFIED = "verified"
        RETIRED = "retired", "Retired (file removed; kept for report history)"

    id = models.CharField(primary_key=True, max_length=255, validators=[reverse_dns])
    app = models.ForeignKey(App, on_delete=models.PROTECT, related_name="recipes")
    runtime = models.CharField(max_length=16, choices=Runtime.choices)
    version_range = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    path = models.CharField(max_length=512, help_text="File path in the whistle-recipes repo.")
    current_revision = models.ForeignKey(
        "RecipeRevision", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["app", "status"])]

    def __str__(self):
        return self.id


class RecipeRevision(models.Model):
    """An immutable snapshot of a recipe file at one commit.

    Compat reports point here, so a rating always refers to the exact steps
    the reporter ran, not to whatever the recipe says today.
    """

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField()
    git_commit = models.CharField(max_length=40, validators=[git_sha])
    spec = models.JSONField(help_text="The recipe, validated against the schema.")
    spec_sha256 = models.CharField(
        max_length=64, validators=[sha256_hex], help_text="Hash of the canonical JSON spec."
    )
    signature = models.TextField(blank=True)
    indexed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["recipe", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["recipe", "number"], name="revision_unique_number"),
            models.UniqueConstraint(fields=["recipe", "spec_sha256"], name="revision_unique_spec"),
        ]

    def __str__(self):
        return f"{self.recipe_id}@{self.number}"


class CompatReport(models.Model):
    """A user's result for one app on one host, via one source or recipe revision."""

    class Rating(models.TextChoices):
        PLATINUM = "platinum"
        GOLD = "gold"
        SILVER = "silver"
        BRONZE = "bronze"
        BROKEN = "broken"

    class HostOS(models.TextChoices):
        LINUX = "linux"
        WINDOWS = "windows"

    class Runtime(models.TextChoices):
        NATIVE = "native"
        FLATPAK = "flatpak"
        APPIMAGE = "appimage"
        WINE = "wine"
        PROTON = "proton"
        WSL = "wsl"
        VM = "vm"

    class GPUVendor(models.TextChoices):
        AMD = "amd"
        INTEL = "intel"
        NVIDIA = "nvidia"
        OTHER = "other"
        UNKNOWN = "unknown"

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="compat_reports")
    # Exactly one of these at creation. Sources can be pruned by ingest, so a
    # report may later lose its source; revisions are never deleted.
    source = models.ForeignKey(
        Source, on_delete=models.SET_NULL, null=True, blank=True, related_name="compat_reports"
    )
    recipe_revision = models.ForeignKey(
        RecipeRevision, on_delete=models.PROTECT, null=True, blank=True, related_name="compat_reports"
    )

    host_os = models.CharField(max_length=16, choices=HostOS.choices)
    distro = models.CharField(max_length=64, blank=True, help_text="os-release ID, e.g. arch, fedora.")
    distro_version = models.CharField(max_length=64, blank=True, help_text="os-release VERSION_ID or Windows build.")
    runtime = models.CharField(max_length=16, choices=Runtime.choices)
    runtime_version = models.CharField(max_length=100, blank=True)
    gpu_vendor = models.CharField(max_length=16, choices=GPUVendor.choices, default=GPUVendor.UNKNOWN)
    rating = models.CharField(max_length=16, choices=Rating.choices)
    notes = models.TextField(blank=True, max_length=5000)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="compat_reports"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(source__isnull=True) | Q(recipe_revision__isnull=True),
                name="report_not_both_source_and_recipe",
            ),
        ]
        indexes = [
            models.Index(fields=["app", "rating"]),
            models.Index(fields=["recipe_revision", "created_at"]),
        ]

    def clean(self):
        if (self.source_id is None) == (self.recipe_revision_id is None):
            raise ValidationError("A report needs exactly one of source or recipe_revision.")
        if self.source_id and self.source.app_id != self.app_id:
            raise ValidationError({"source": "Source belongs to a different app."})
        if self.recipe_revision_id:
            recipe = self.recipe_revision.recipe
            if recipe.app_id != self.app_id:
                raise ValidationError({"recipe_revision": "Recipe belongs to a different app."})
            if recipe.runtime != self.runtime:
                raise ValidationError({"runtime": f"Recipe runs on {recipe.runtime}."})
        if self.host_os == self.HostOS.LINUX and not self.distro:
            raise ValidationError({"distro": "Required for Linux hosts."})

    def __str__(self):
        return f"{self.app_id}: {self.rating} on {self.distro or self.host_os}"
