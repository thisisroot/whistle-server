from django.contrib import admin

from .models import App, CompatReport, Recipe, RecipeRevision, Source


class SourceInline(admin.TabularInline):
    model = Source
    extra = 0
    fields = ["kind", "ref", "platforms", "version", "origin", "locked"]


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "license", "winget_id", "updated_at"]
    search_fields = ["id", "name", "appstream_id", "repology_name", "winget_id"]
    inlines = [SourceInline]


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ["app", "kind", "ref", "version", "origin", "locked", "last_seen_at"]
    list_filter = ["kind", "origin", "locked"]
    search_fields = ["app__id", "app__name", "ref"]
    raw_id_fields = ["app"]


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ["id", "app", "runtime", "status", "current_revision", "updated_at"]
    list_filter = ["runtime", "status"]
    search_fields = ["id", "app__name"]
    raw_id_fields = ["app", "current_revision"]


@admin.register(RecipeRevision)
class RecipeRevisionAdmin(admin.ModelAdmin):
    list_display = ["__str__", "git_commit", "indexed_at"]
    search_fields = ["recipe__id", "git_commit"]
    readonly_fields = ["recipe", "number", "git_commit", "spec", "spec_sha256", "indexed_at"]


@admin.register(CompatReport)
class CompatReportAdmin(admin.ModelAdmin):
    list_display = ["app", "rating", "runtime", "host_os", "distro", "gpu_vendor", "created_at"]
    list_filter = ["rating", "runtime", "host_os", "gpu_vendor"]
    search_fields = ["app__id", "app__name", "notes"]
    raw_id_fields = ["app", "source", "recipe_revision", "created_by"]
