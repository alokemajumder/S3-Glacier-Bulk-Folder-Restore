"""Tkinter desktop front end (GitHub issue #1).

Wraps the same engine the CLI uses, so the two never drift apart.

Threading contract
------------------
Tk is not thread-safe: only the main thread may touch a widget. The restore
runs on a worker thread and communicates **exclusively** by pushing events
onto a :class:`queue.Queue`, which the main thread drains from an ``after()``
timer. Nothing in :class:`GuiRunner` imports or touches Tk, which is also what
makes it testable without a display.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .aws import AwsError, build_client, build_session, resolve_bucket_region, verify_access
from .config import ConfigError, RestoreConfig
from .engine import RestoreEngine, summarize
from .filters import KeyFilter, load_pattern_file
from .lister import format_bytes, sample_objects
from .manifest import NullManifest
from .models import RETRIEVAL_TIERS
from .state import NullState, StateFile

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised by the availability test
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - headless / python built without Tk
    TK_AVAILABLE = False

WINDOW_TITLE = "S3 Glacier Bulk Folder Restore"
SAMPLE_LIMIT = 25


# ---------------------------------------------------------------- plumbing --


class QueueLogHandler(logging.Handler):
    """Routes log records to a queue instead of a stream."""

    def __init__(self, sink: queue.Queue) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put(("log", (record.levelno, self.format(record))))
        except Exception:  # pragma: no cover - a logging failure must not cascade
            self.handleError(record)


@dataclass
class Fields:
    """Raw form values, before validation."""

    bucket: str = ""
    prefix: str = ""
    days: str = "30"
    tier: str = "Bulk"
    concurrency: str = "16"
    region: str = ""
    profile: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    skip_file: str = ""
    exclude: str = ""
    dry_run: bool = True
    versions: bool = False
    include_intelligent_tiering: bool = False
    state_file: str = ""


def config_from_fields(fields: Fields) -> RestoreConfig:
    """Turn form values into a validated config, or raise ``ConfigError``.

    Kept free of Tk so the validation rules can be tested directly.
    """
    if not fields.bucket.strip():
        raise ConfigError("Enter a bucket name.")

    def as_int(value: str, label: str, default: int) -> int:
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            raise ConfigError(f"{label} must be a whole number (got '{text}').") from None

    excludes = [p.strip() for p in fields.exclude.split(",") if p.strip()]
    if fields.skip_file.strip():
        try:
            excludes.extend(load_pattern_file(fields.skip_file.strip()))
        except OSError as exc:
            raise ConfigError(f"Could not read the skip-list file: {exc}") from exc

    cfg = RestoreConfig(
        bucket=fields.bucket.strip(),
        prefix=fields.prefix.strip(),
        days=as_int(fields.days, "Restore days", 30),
        tier=fields.tier or "Bulk",
        concurrency=as_int(fields.concurrency, "Concurrency", 16),
        region=fields.region.strip() or None,
        profile=fields.profile.strip() or None,
        access_key_id=fields.access_key_id.strip() or None,
        secret_access_key=fields.secret_access_key.strip() or None,
        excludes=excludes,
        dry_run=fields.dry_run,
        versions=fields.versions,
        include_intelligent_tiering=fields.include_intelligent_tiering,
        state_file=fields.state_file.strip() or None,
    )
    cfg.validate()
    return cfg


class GuiRunner:
    """Runs bucket checks and restores off the main thread.

    Every result is delivered as an ``(kind, payload)`` event on ``events``:

    ``("log", (levelno, text))``   a formatted log line
    ``("status", text)``           one-line status for the status bar
    ``("samples", [S3Object])``    preview rows from a bucket check
    ``("checked", region)``        the bucket check succeeded
    ``("summary", [str])``         end-of-run summary lines
    ``("error", text)``            fatal, already human-readable
    ``("finished", stats|None)``   always last for any operation
    """

    def __init__(self, client_factory: Callable[[RestoreConfig], Any] | None = None) -> None:
        self.events: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_factory = client_factory or self._default_client_factory

    # -- lifecycle ---------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self.stop.set()

    def check(self, cfg: RestoreConfig) -> None:
        self._spawn(self._check, cfg, name="check")

    def restore(self, cfg: RestoreConfig) -> None:
        self._spawn(self._restore, cfg, name="restore")

    def _spawn(self, target, cfg: RestoreConfig, name: str) -> None:
        if self.busy:
            raise RuntimeError("An operation is already running.")
        self.stop.clear()
        self._thread = threading.Thread(target=self._guard, args=(target, cfg), name=name)
        self._thread.daemon = True
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the worker. Used by tests; the GUI never blocks on this."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _guard(self, target, cfg: RestoreConfig) -> None:
        try:
            target(cfg)
        except (AwsError, ConfigError) as exc:
            self.events.put(("error", str(exc)))
            self.events.put(("finished", None))
        except Exception as exc:  # noqa: BLE001 - a crash must reach the user, not stderr
            log.debug("GUI worker failed", exc_info=True)
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))
            self.events.put(("finished", None))

    # -- operations --------------------------------------------------------

    @staticmethod
    def _default_client_factory(cfg: RestoreConfig):
        session = build_session(cfg)
        region = resolve_bucket_region(session, cfg)
        cfg.region = region
        client = build_client(session, cfg, region)
        verify_access(client, cfg)
        return client

    def _check(self, cfg: RestoreConfig) -> None:
        self.events.put(("status", f"Connecting to '{cfg.bucket}'..."))
        client = self._client_factory(cfg)
        self.events.put(("status", f"Connected. Listing '{cfg.prefix or '(bucket root)'}'..."))

        samples, has_more = sample_objects(client, cfg, limit=SAMPLE_LIMIT)
        self.events.put(("samples", (samples, has_more)))

        if samples:
            suffix = "+" if has_more else ""
            self.events.put(("status", f"Found {len(samples)}{suffix} object(s)."))
        else:
            self.events.put(("status", "No objects found under that prefix."))
        self.events.put(("checked", cfg.region))
        self.events.put(("finished", None))

    def _restore(self, cfg: RestoreConfig) -> None:
        client = self._client_factory(cfg)
        key_filter = KeyFilter(cfg.excludes, cfg.includes, cfg.ignore_case)
        state = StateFile(cfg.state_file, bucket=cfg.bucket) if cfg.state_file else NullState()
        resumed = state.load()
        if resumed:
            self.events.put(("status", f"Resuming: {resumed:,} object(s) already done."))

        self.events.put(("status", "Dry run in progress..." if cfg.dry_run else "Restoring..."))
        with state:
            engine = RestoreEngine(
                client,
                cfg,
                key_filter=key_filter,
                state=state,
                manifest=NullManifest(),
                stop_event=self.stop,
            )
            stats = engine.run()
            elapsed = engine.elapsed

        self.events.put(("summary", summarize(stats, cfg, elapsed)))
        if self.stop.is_set():
            self.events.put(("status", "Stopped before completion."))
        elif cfg.dry_run:
            self.events.put(("status", f"Dry run complete: {stats.actions:,} would restore."))
        else:
            self.events.put(("status", f"Done: {stats.actions:,} restore(s) requested."))
        self.events.put(("finished", stats))


# ------------------------------------------------------------------ widgets --


if TK_AVAILABLE:  # pragma: no cover - requires a display

    class RestoreApp(tk.Tk):
        """The main window."""

        POLL_MS = 120

        def __init__(self, runner: GuiRunner | None = None) -> None:
            super().__init__()
            self.title(WINDOW_TITLE)
            self.minsize(880, 640)
            self.runner = runner or GuiRunner()
            self._checked = False
            self._build_widgets()
            self._attach_logging()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(self.POLL_MS, self._drain)

        # -- construction --------------------------------------------------

        def _build_widgets(self) -> None:
            self.columnconfigure(0, weight=1)
            self.rowconfigure(2, weight=1)
            self.rowconfigure(3, weight=2)

            self._build_form()
            self._build_buttons()
            self._build_preview()
            self._build_log()
            self._build_status()

        def _build_form(self) -> None:
            notebook = ttk.Notebook(self)
            notebook.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

            basic = ttk.Frame(notebook, padding=10)
            advanced = ttk.Frame(notebook, padding=10)
            notebook.add(basic, text="Restore")
            notebook.add(advanced, text="Filters and credentials")
            basic.columnconfigure(1, weight=1)
            advanced.columnconfigure(1, weight=1)

            self.var = {
                "bucket": tk.StringVar(),
                "prefix": tk.StringVar(),
                "days": tk.StringVar(value="30"),
                "tier": tk.StringVar(value="Bulk"),
                "concurrency": tk.StringVar(value="16"),
                "region": tk.StringVar(),
                "profile": tk.StringVar(),
                "access_key_id": tk.StringVar(),
                "secret_access_key": tk.StringVar(),
                "skip_file": tk.StringVar(),
                "exclude": tk.StringVar(),
                "state_file": tk.StringVar(),
                "dry_run": tk.BooleanVar(value=True),
                "versions": tk.BooleanVar(value=False),
                "intelligent": tk.BooleanVar(value=False),
                "show_secret": tk.BooleanVar(value=False),
            }

            row = 0
            self._labelled_entry(basic, row, "S3 bucket", "bucket")
            row += 1
            self._labelled_entry(
                basic, row, "Prefix (folder)", "prefix", hint="blank = whole bucket"
            )
            row += 1

            numbers = ttk.Frame(basic)
            numbers.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 0))
            ttk.Label(numbers, text="Keep restored for").pack(side="left")
            ttk.Spinbox(numbers, from_=1, to=3650, width=6, textvariable=self.var["days"]).pack(
                side="left", padx=(6, 2)
            )
            ttk.Label(numbers, text="day(s)   Tier").pack(side="left", padx=(8, 0))
            ttk.Combobox(
                numbers,
                values=list(RETRIEVAL_TIERS),
                textvariable=self.var["tier"],
                state="readonly",
                width=10,
            ).pack(side="left", padx=6)
            ttk.Label(numbers, text="Parallel requests").pack(side="left", padx=(8, 0))
            ttk.Spinbox(
                numbers, from_=1, to=256, width=5, textvariable=self.var["concurrency"]
            ).pack(side="left", padx=6)
            row += 1

            checks = ttk.Frame(basic)
            checks.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk.Checkbutton(
                checks,
                text="Dry run (preview only, nothing is restored)",
                variable=self.var["dry_run"],
            ).pack(side="left")
            ttk.Checkbutton(checks, text="All object versions", variable=self.var["versions"]).pack(
                side="left", padx=(14, 0)
            )
            ttk.Checkbutton(
                checks,
                text="Include Intelligent-Tiering",
                variable=self.var["intelligent"],
            ).pack(side="left", padx=(14, 0))

            # --- advanced tab
            row = 0
            self._labelled_entry(
                advanced, row, "Exclude patterns", "exclude", hint="comma separated globs"
            )
            row += 1
            self._file_row(advanced, row, "Skip-list file", "skip_file", save=False)
            row += 1
            self._file_row(advanced, row, "State file (resume)", "state_file", save=True)
            row += 1
            ttk.Separator(advanced, orient="horizontal").grid(
                row=row, column=0, columnspan=3, sticky="ew", pady=10
            )
            row += 1
            ttk.Label(
                advanced,
                text="Leave credentials blank to use the default AWS credential chain "
                "(environment, ~/.aws, or an instance role).",
                foreground="#666666",
                wraplength=760,
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row += 1
            self._labelled_entry(advanced, row, "AWS profile", "profile")
            row += 1
            self._labelled_entry(advanced, row, "Region", "region", hint="auto-detected")
            row += 1
            self._labelled_entry(advanced, row, "Access key ID", "access_key_id")
            row += 1

            ttk.Label(advanced, text="Secret access key").grid(
                row=row, column=0, sticky="w", pady=3
            )
            self.secret_entry = ttk.Entry(
                advanced, textvariable=self.var["secret_access_key"], show="•"
            )
            self.secret_entry.grid(row=row, column=1, sticky="ew", padx=6, pady=3)
            ttk.Checkbutton(
                advanced, text="Show", variable=self.var["show_secret"], command=self._toggle_secret
            ).grid(row=row, column=2, sticky="w")

        def _labelled_entry(self, parent, row, label, key, hint=""):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(parent, textvariable=self.var[key]).grid(
                row=row, column=1, sticky="ew", padx=6, pady=3
            )
            if hint:
                ttk.Label(parent, text=hint, foreground="#666666").grid(
                    row=row, column=2, sticky="w"
                )

        def _file_row(self, parent, row, label, key, save):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(parent, textvariable=self.var[key]).grid(
                row=row, column=1, sticky="ew", padx=6, pady=3
            )
            ttk.Button(parent, text="Browse...", command=lambda: self._browse(key, save)).grid(
                row=row, column=2, sticky="w"
            )

        def _build_buttons(self) -> None:
            bar = ttk.Frame(self)
            bar.grid(row=1, column=0, sticky="ew", padx=10, pady=6)
            self.check_button = ttk.Button(bar, text="Check bucket", command=self._on_check)
            self.check_button.pack(side="left")
            self.start_button = ttk.Button(bar, text="Start", command=self._on_start)
            self.start_button.pack(side="left", padx=6)
            self.stop_button = ttk.Button(bar, text="Stop", command=self._on_stop, state="disabled")
            self.stop_button.pack(side="left")
            ttk.Button(bar, text="Save log...", command=self._on_save_log).pack(side="right")
            ttk.Button(bar, text="Clear log", command=self._clear_log).pack(side="right", padx=6)

        def _build_preview(self) -> None:
            frame = ttk.LabelFrame(self, text="Preview", padding=6)
            frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)

            columns = ("key", "class", "size")
            self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=6)
            for name, title, width in (
                ("key", "Key", 560),
                ("class", "Storage class", 150),
                ("size", "Size", 110),
            ):
                self.tree.heading(name, text=title)
                self.tree.column(name, width=width, anchor="w" if name == "key" else "e")
            self.tree.grid(row=0, column=0, sticky="nsew")
            bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
            bar.grid(row=0, column=1, sticky="ns")
            self.tree.configure(yscrollcommand=bar.set)

        def _build_log(self) -> None:
            frame = ttk.LabelFrame(self, text="Activity", padding=6)
            frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=4)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            self.log_view = scrolledtext.ScrolledText(
                frame, wrap="word", height=12, state="disabled"
            )
            self.log_view.grid(row=0, column=0, sticky="nsew")
            self.log_view.tag_configure("WARNING", foreground="#b8860b")
            self.log_view.tag_configure("ERROR", foreground="#c0392b")
            self.log_view.tag_configure("SUMMARY", font=("TkFixedFont", 10))

        def _build_status(self) -> None:
            bar = ttk.Frame(self)
            bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))
            bar.columnconfigure(0, weight=1)
            self.status = tk.StringVar(value="Ready.")
            ttk.Label(bar, textvariable=self.status, anchor="w").grid(row=0, column=0, sticky="ew")
            self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
            self.progress.grid(row=0, column=1, sticky="e")

        def _attach_logging(self) -> None:
            handler = QueueLogHandler(self.runner.events)
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.setLevel(logging.INFO)
            root = logging.getLogger("s3_glacier_restore")
            root.setLevel(logging.INFO)
            root.addHandler(handler)
            self._log_handler = handler

        # -- actions -------------------------------------------------------

        def _fields(self) -> Fields:
            return Fields(
                bucket=self.var["bucket"].get(),
                prefix=self.var["prefix"].get(),
                days=self.var["days"].get(),
                tier=self.var["tier"].get(),
                concurrency=self.var["concurrency"].get(),
                region=self.var["region"].get(),
                profile=self.var["profile"].get(),
                access_key_id=self.var["access_key_id"].get(),
                secret_access_key=self.var["secret_access_key"].get(),
                skip_file=self.var["skip_file"].get(),
                exclude=self.var["exclude"].get(),
                state_file=self.var["state_file"].get(),
                dry_run=self.var["dry_run"].get(),
                versions=self.var["versions"].get(),
                include_intelligent_tiering=self.var["intelligent"].get(),
            )

        def _config(self) -> RestoreConfig | None:
            try:
                return config_from_fields(self._fields())
            except ConfigError as exc:
                messagebox.showerror("Check the settings", str(exc))
                return None

        def _on_check(self) -> None:
            cfg = self._config()
            if cfg is None or self.runner.busy:
                return
            self.tree.delete(*self.tree.get_children())
            self._set_running(True)
            self.runner.check(cfg)

        def _on_start(self) -> None:
            cfg = self._config()
            if cfg is None or self.runner.busy:
                return
            if not cfg.dry_run and not self._confirm(cfg):
                return
            self._set_running(True)
            self.runner.restore(cfg)

        def _confirm(self, cfg: RestoreConfig) -> bool:
            return messagebox.askyesno(
                "Start a live restore?",
                f"This will issue billable {cfg.tier}-tier retrieval requests for every "
                f"archived object under:\n\n"
                f"    s3://{cfg.bucket}/{cfg.prefix}\n\n"
                f"Restored copies are billed at S3 Standard rates for {cfg.days} day(s) "
                "on top of your archive storage.\n\n"
                "Run a dry run first if you are unsure. Continue?",
                icon="warning",
                default="no",
            )

        def _on_stop(self) -> None:
            self.runner.cancel()
            self.status.set("Stopping after the in-flight requests finish...")
            self.stop_button.configure(state="disabled")

        def _on_save_log(self) -> None:
            path = filedialog.asksaveasfilename(
                title="Save activity log",
                defaultextension=".log",
                filetypes=[("Log files", "*.log"), ("All files", "*.*")],
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_view.get("1.0", "end"))
            self.status.set(f"Log saved to {path}")

        def _browse(self, key: str, save: bool) -> None:
            chooser = filedialog.asksaveasfilename if save else filedialog.askopenfilename
            path = chooser(title="Select a file", initialdir=os.getcwd())
            if path:
                self.var[key].set(path)

        def _toggle_secret(self) -> None:
            self.secret_entry.configure(show="" if self.var["show_secret"].get() else "•")

        def _clear_log(self) -> None:
            self.log_view.configure(state="normal")
            self.log_view.delete("1.0", "end")
            self.log_view.configure(state="disabled")

        def _set_running(self, running: bool) -> None:
            state = "disabled" if running else "normal"
            self.check_button.configure(state=state)
            self.start_button.configure(state=state)
            self.stop_button.configure(state="normal" if running else "disabled")
            if running:
                self.progress.start(12)
            else:
                self.progress.stop()

        # -- event pump ----------------------------------------------------

        def _drain(self) -> None:
            """Pull worker events on the main thread. Widgets are touched only here."""
            try:
                while True:
                    kind, payload = self.runner.events.get_nowait()
                    self._handle(kind, payload)
            except queue.Empty:
                pass
            finally:
                self.after(self.POLL_MS, self._drain)

        def _handle(self, kind: str, payload) -> None:
            if kind == "log":
                level, text = payload
                self._append(text, logging.getLevelName(level))
            elif kind == "status":
                self.status.set(payload)
            elif kind == "samples":
                self._show_samples(*payload)
            elif kind == "summary":
                self._append("\n".join(payload), "SUMMARY")
            elif kind == "checked":
                self._checked = True
            elif kind == "error":
                self._append(payload, "ERROR")
                self.status.set("Failed.")
                messagebox.showerror(WINDOW_TITLE, payload)
            elif kind == "finished":
                self._set_running(False)

        def _show_samples(self, samples, has_more: bool) -> None:
            self.tree.delete(*self.tree.get_children())
            for obj in samples:
                self.tree.insert(
                    "", "end", values=(obj.key, obj.storage_class, format_bytes(obj.size))
                )
            if has_more:
                self.tree.insert("", "end", values=("... more objects not shown", "", ""))

        def _append(self, text: str, tag: str = "") -> None:
            self.log_view.configure(state="normal")
            self.log_view.insert(
                "end", text + "\n", tag if tag in ("WARNING", "ERROR", "SUMMARY") else ""
            )
            self.log_view.see("end")
            self.log_view.configure(state="disabled")

        def _on_close(self) -> None:
            if self.runner.busy and not messagebox.askokcancel(
                WINDOW_TITLE,
                "A restore is still running. Requests already sent to AWS will "
                "continue regardless. Close anyway?",
            ):
                return
            self.runner.cancel()
            logging.getLogger("s3_glacier_restore").removeHandler(self._log_handler)
            self.destroy()


def main(argv=None) -> int:
    """Entry point for ``s3-glacier-restore-gui``."""
    if not TK_AVAILABLE:
        import sys

        sys.stderr.write(
            "Tkinter is not available in this Python installation.\n"
            "Install it (Debian/Ubuntu: 'apt install python3-tk', macOS: use\n"
            "python.org or 'brew install python-tk') or use the command line:\n"
            "  s3-glacier-restore --help\n"
        )
        return 2

    RestoreApp().mainloop()  # pragma: no cover - requires a display
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
