import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog.index import import_index


class Command(BaseCommand):
    help = "Import hand-maintained apps and sources from an index-format JSON file."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)

    def handle(self, path, **options):
        try:
            apps, sources = import_index(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"imported {apps} apps, {sources} sources")
