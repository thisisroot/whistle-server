from django.core.management.base import BaseCommand, CommandError

from catalog.appstream import FEEDS, ingest_appstream


class Command(BaseCommand):
    help = "Add native packages (apt, pacman, dnf, zypper) from the distros' AppStream data."

    def add_arguments(self, parser):
        parser.add_argument("--only", nargs="+", choices=[f.name for f in FEEDS], help="feeds to use")
        parser.add_argument("--strict", action="store_true", help="fail if any feed fails")

    def handle(self, only, strict, **options):
        feeds = [f for f in FEEDS if not only or f.name in only]
        results = ingest_appstream(feeds, log=lambda msg: self.stdout.write(msg))
        for name, result in results.items():
            self.stdout.write(f"{name}: {result if isinstance(result, str) else f'{result} apps matched'}")
        failed = [name for name, result in results.items() if isinstance(result, str)]
        if failed and strict:
            raise CommandError(f"feeds failed: {', '.join(failed)}")
