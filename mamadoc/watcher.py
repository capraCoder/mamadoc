"""Watch mamadoc folder for new PDFs and auto-process them."""

import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import db
from .config import MAMADOC_DIR, check_setup, setup_logging
from .process_pdf import process_pdf

log = setup_logging()

RETRY_ATTEMPTS = 2
RETRY_DELAY = 30  # seconds between retry attempts


class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".pdf"):
            path = Path(event.src_path)
            log.info(f"New PDF detected: {path.name}")
            time.sleep(3)

            # Wait until file size stabilizes (scanner may still be writing)
            prev_size = -1
            for _ in range(10):
                try:
                    size = path.stat().st_size
                except OSError:
                    log.warning(f"  File locked, waiting: {path.name}")
                    time.sleep(2)
                    continue
                if size == prev_size and size > 0:
                    break
                prev_size = size
                time.sleep(1)

            # Retry loop
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    result = process_pdf(path)
                    if result:
                        log.info(
                            f"  Processed: {result.get('doc_type')} — "
                            f"{result.get('subject')}"
                        )
                    else:
                        log.warning(f"  Skipped: {path.name}")
                    return  # success — exit retry loop
                except Exception as e:
                    log.error(
                        f"  Attempt {attempt}/{RETRY_ATTEMPTS} failed for "
                        f"{path.name}: {e}"
                    )
                    if attempt < RETRY_ATTEMPTS:
                        log.info(f"  Retrying in {RETRY_DELAY}s...")
                        time.sleep(RETRY_DELAY)

            log.error(f"  GAVE UP on {path.name} after {RETRY_ATTEMPTS} attempts")


def main():
    if not check_setup():
        sys.exit(1)

    db.init_db()

    observer = Observer()
    observer.schedule(PDFHandler(), str(MAMADOC_DIR), recursive=False)
    observer.start()

    log.info(f"Watching {MAMADOC_DIR} for new PDFs...")
    log.info("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping watcher...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
