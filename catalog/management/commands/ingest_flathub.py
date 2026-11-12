from django.core.management.base import BaseCommand

from catalog.appstream import ingest_flathub_media
from catalog.ingest import ingest_flathub


class Command(BaseCommand):
    help = "Import apps from Flathub, most popular first."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="import only the N most popular (default: all)")
        parser.add_argument("--no-media", action="store_true", help="skip screenshots and homepages")

    def handle(self, limit, no_media, **options):
        seen, skipped = ingest_flathub(limit=limit or None)
        self.stdout.write(f"imported {seen} Flathub apps, skipped {skipped}")
        if not no_media:
            filled = ingest_flathub_media()
            self.stdout.write(f"{filled} Flathub apps gained screenshots or a homepage")
