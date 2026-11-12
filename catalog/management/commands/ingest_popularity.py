from django.core.management.base import BaseCommand

from catalog.popularity import ingest_popularity


class Command(BaseCommand):
    help = "Rate how widely each app is used, from Flathub installs and Debian's popcon."

    def handle(self, **options):
        for source, result in ingest_popularity(log=self.stdout.write).items():
            self.stdout.write(f"{source}: {result if isinstance(result, str) else f'{result} apps rated'}")
