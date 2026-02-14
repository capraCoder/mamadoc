"""Mamadoc CLI entry point."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="mamadoc",
        description="AI-powered document tracker with structured extraction",
    )
    sub = parser.add_subparsers(dest="command")

    # mamadoc dashboard
    sub.add_parser("dashboard", help="Launch the Streamlit dashboard")

    # mamadoc process [pdf] [--force]
    p_proc = sub.add_parser("process", help="Process PDF(s)")
    p_proc.add_argument("pdf", nargs="?", help="Single PDF to process")
    p_proc.add_argument(
        "--force", action="store_true", help="Reprocess even if already done"
    )

    # mamadoc watch
    sub.add_parser("watch", help="Watch folder for new PDFs and auto-process")

    # mamadoc start
    sub.add_parser(
        "start", help="Start watcher + dashboard together (drop PDFs → auto-process)"
    )

    # mamadoc check
    sub.add_parser("check", help="Verify environment setup")

    args = parser.parse_args()

    if args.command == "dashboard":
        import subprocess

        from .config import MAMADOC_DIR

        subprocess.run(
            [sys.executable, "-m", "streamlit", "run", "mamadoc/app.py"],
            cwd=str(MAMADOC_DIR),
        )

    elif args.command == "process":
        from . import db
        from .config import check_setup
        from .process_pdf import process_all, process_pdf

        if not check_setup():
            sys.exit(1)
        db.init_db()

        if args.pdf:
            from pathlib import Path

            result = process_pdf(Path(args.pdf), force=args.force)
            if result:
                print(f"Done: {result.get('doc_type')} — {result.get('subject')}")
        else:
            results, failed = process_all(force=args.force)
            print(f"Processed: {len(results)}, Failed: {len(failed)}")

    elif args.command == "watch":
        from .watcher import main as watch_main

        watch_main()

    elif args.command == "start":
        import subprocess

        from . import db
        from .config import MAMADOC_DIR, check_setup

        if not check_setup():
            sys.exit(1)
        db.init_db()

        # Watcher in background thread
        from .watcher import PDFHandler

        from watchdog.observers import Observer

        observer = Observer()
        observer.schedule(PDFHandler(), str(MAMADOC_DIR), recursive=False)
        observer.daemon = True
        observer.start()
        print(f"Watcher started on {MAMADOC_DIR}")

        # Dashboard in foreground
        try:
            subprocess.run(
                [sys.executable, "-m", "streamlit", "run", "mamadoc/app.py"],
                cwd=str(MAMADOC_DIR),
            )
        finally:
            observer.stop()
            observer.join()

    elif args.command == "check":
        from .config import check_setup

        if check_setup():
            print("All good.")
        else:
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
