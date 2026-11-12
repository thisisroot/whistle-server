import json
from pathlib import Path

from django.core.management.base import BaseCommand

from catalog.index import build_index
from catalog.models import Recipe, Source


class Command(BaseCommand):
    help = "Write the catalog index that clients download."

    def add_arguments(self, parser):
        parser.add_argument("output", type=Path, help="where to write the index")
        parser.add_argument(
            "--exclude-status",
            action="append",
            default=[],
            choices=[s.value for s in Recipe.Status],
            help="leave out recipes with this status (for older clients); repeatable",
        )
        parser.add_argument(
            "--exclude-kind",
            action="append",
            default=[],
            choices=[k.value for k in Source.Kind],
            help="leave out sources of this kind; repeatable",
        )

    def handle(self, output, exclude_status, exclude_kind, **options):
        index = build_index(exclude_statuses=exclude_status, exclude_kinds=exclude_kind)
        tmp = output.with_suffix(output.suffix + ".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n")
        tmp.replace(output)
        self.stdout.write(f"wrote {len(index['apps'])} apps to {output}")
