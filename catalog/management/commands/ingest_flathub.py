from django.core.management.base import BaseCommand

from catalog.ingest import ingest_flathub


class Command(BaseCommand):
    help = "Import the most popular apps from Flathub."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, limit, **options):
        seen, skipped = ingest_flathub(limit=limit)
        self.stdout.write(f"imported {seen} Flathub apps, skipped {skipped}")
