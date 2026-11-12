from django.core.management.base import BaseCommand

from catalog.winget import checkout, ingest_winget


class Command(BaseCommand):
    help = "Add Windows apps from winget, as untested Wine recipes."

    def add_arguments(self, parser):
        parser.add_argument("repo", help="where to keep the winget-pkgs checkout (cloned if missing)")
        parser.add_argument("--no-update", action="store_true", help="use the checkout as it is")
        parser.add_argument("--limit", type=int, default=0, help="only the first N packages (for testing)")

    def handle(self, repo, no_update, limit, **options):
        log = self.stdout.write
        if no_update:
            import subprocess

            commit = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
        else:
            commit = checkout(repo, log=log)
        counts = ingest_winget(f"{repo}/manifests", commit, limit=limit or None, log=log)
        self.stdout.write(
            f"winget {commit[:12]}: {counts['recipes']} recipes, {counts['apps']} new apps, "
            f"{counts.get('merged', 0)} merged into the same app's Linux entry, "
            f"{counts['skipped']} packages without an installer Wine can run, "
            f"{counts['invalid']} invalid, {counts.get('retired', 0)} retired"
        )
