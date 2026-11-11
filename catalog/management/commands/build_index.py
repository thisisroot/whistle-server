import json
from pathlib import Path

from django.core.management.base import BaseCommand

from catalog.index import build_index


class Command(BaseCommand):
    help = "Write the catalog index that clients download."

    def add_arguments(self, parser):
        parser.add_argument("output", type=Path, help="where to write index.json")

    def handle(self, output, **options):
        index = build_index()
        tmp = output.with_suffix(output.suffix + ".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n")
        tmp.replace(output)
        self.stdout.write(f"wrote {len(index['apps'])} apps to {output}")
