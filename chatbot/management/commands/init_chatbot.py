from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create LangGraph checkpoint tables (checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations)'

    def handle(self, *args, **options):
        engine = settings.DATABASES["default"].get("ENGINE", "")
        db = settings.DATABASES["default"]

        # ── PostgreSQL ────────────────────────────────────────────────────────
        if "postgresql" in engine or "psycopg" in engine:
            try:
                import psycopg
                from langgraph.checkpoint.postgres import PostgresSaver

                user = db.get("USER", "")
                password = db.get("PASSWORD", "")
                host = db.get("HOST", "localhost")
                port = db.get("PORT", "5432")
                name = db.get("NAME", "")
                dsn = f"postgresql://{user}:{password}@{host}:{port}/{name}"

                self.stdout.write(f"Connecting to PostgreSQL: {host}:{port}/{name} ...")

                with psycopg.connect(dsn, autocommit=True) as conn:
                    checkpointer = PostgresSaver(conn)
                    checkpointer.setup()

                self.stdout.write(self.style.SUCCESS(
                    "✅ Checkpoint tables created successfully in PostgreSQL:\n"
                    "   - checkpoints\n"
                    "   - checkpoint_blobs\n"
                    "   - checkpoint_writes\n"
                    "   - checkpoint_migrations"
                ))

            except ImportError as e:
                self.stderr.write(self.style.ERROR(
                    f"Missing package: {e}\n"
                    "Run: pip install \"psycopg[binary,pool]\""
                ))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error creating PostgreSQL tables: {e}"))

        # ── SQLite ────────────────────────────────────────────────────────────
        elif "sqlite" in engine:
            try:
                import sqlite3
                from langgraph.checkpoint.sqlite import SqliteSaver

                db_path = db.get("NAME", "db.sqlite3")
                self.stdout.write(f"Initializing SQLite checkpointer at: {db_path} ...")

                # Create connection and setup checkpointer
                with sqlite3.connect(db_path) as conn:
                    checkpointer = SqliteSaver(conn)
                    checkpointer.setup()

                self.stdout.write(self.style.SUCCESS(
                    "✅ Checkpoint tables created successfully in SQLite:\n"
                    f"   - Database: {db_path}\n"
                    "   - checkpoints\n"
                    "   - checkpoint_blobs\n"
                    "   - checkpoint_writes\n"
                    "   - checkpoint_migrations"
                ))

            except ImportError as e:
                self.stderr.write(self.style.ERROR(
                    f"Missing package: {e}\n"
                    "Run: pip install langgraph-checkpoint-sqlite"
                ))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error creating SQLite checkpoint tables: {e}"))

        # ── Other databases ───────────────────────────────────────────────────
        else:
            self.stdout.write(self.style.WARNING(
                "Database is not PostgreSQL or SQLite. Using MemorySaver — no tables needed."
            ))
