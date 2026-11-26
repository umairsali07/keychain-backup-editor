"""
Keychain Backup Editor - a GUI around irestore.

Lets you:
  * pick an iTunes/Finder backup folder and enter the password ONCE
  * view the keychain items in a searchable table (access group / service / account)
  * delete items (selected rows, or every item in an access group)
  * optionally edit a field value on an item
  * rebuild a restorable backup

The password is supplied to irestore through the IRESTORE_PASSWORD environment
variable, so you are only asked for it one time. After the first command the
app switches to the pre-derived hex key irestore prints, to skip the slow key
derivation on later steps.

Requires irestore.exe (the patched build) next to this script.
Standard library only (tkinter).
"""

import base64
import json
import os
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# When frozen by PyInstaller, bundled files live in a temp dir (sys._MEIPASS),
# while output should go next to the .exe. When run as a plain script, both are
# the script's own folder.
if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = Path(__file__).resolve().parent
    APP_DIR = RESOURCE_DIR

IRESTORE = RESOURCE_DIR / "irestore.exe"

# dumpkeys JSON group name  <->  keychain-backup.plist section name
JSON_TO_PLIST = {"General": "genp", "Internet": "inet", "Certs": "cert", "Keys": "keys"}
PLIST_TO_JSON = {v: k for k, v in JSON_TO_PLIST.items()}

# Fields shown as columns and offered for search/replace.
COLUMNS = ("group", "agrp", "svce", "acct", "class")
DERIVED_KEY_RE = re.compile(r"use the password ([0-9a-f]{64}) to skip")


def as_text(v):
    """Make a dump-JSON field printable."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return base64.b64encode(bytes(v)).decode()
    return str(v)


class EditorApp:
    def __init__(self, root):
        self.root = root
        root.title("Keychain Backup Editor")
        root.geometry("1150x700")

        self.backup_dir = tk.StringVar()
        self.password = tk.StringVar()
        self.search = tk.StringVar()
        # Where every run's folder is created, and an optional name for this run.
        self.out_base = tk.StringVar(value=str(APP_DIR / "KeychainEditor-Runs"))
        self.run_name = tk.StringVar()
        self.status = tk.StringVar(value="Select a backup folder, enter the password, then Load.")

        # Runtime state
        self.work_dir = None
        self.original_dump = None      # dict: group -> list of records (unedited)
        self.records = []              # flat: {"group","ref","rec"}
        self.deleted = set()           # ref (base64) marked for deletion
        self.edits = {}                # ref -> edited record (changed field values)
        self.env_password = None       # value used for IRESTORE_PASSWORD
        self.log_q = queue.Queue()
        self.busy = False

        self._build_ui()
        self.root.after(100, self._drain_log)

        # Hidden demo mode for documentation screenshots (synthetic data only).
        # Set KBE_DEMO=1 for the main window, KBE_DEMO=edit to also open the editor.
        if os.environ.get("KBE_DEMO"):
            self.root.after(250, self._load_demo)

    def _load_demo(self):
        import base64 as _b64
        import hashlib as _h
        def mk(agrp, svce, acct):
            raw = _h.sha1((agrp + "|" + svce + "|" + acct).encode()).digest()
            return {
                "agrp": agrp, "svce": svce, "acct": acct,
                "labl": svce.split(".")[-1], "pdmn": "ak", "_class": 7,
                "cdat": "2026-11-20T10:00:00Z", "mdat": "2026-11-25T18:30:00Z",
                "_ref": _b64.b64encode(raw).decode(),
                "_wkey": _b64.b64encode(b"wrapped-key-bytes").decode(),
                "sha1": _b64.b64encode(raw[:20]).decode(),
                "v_Data": _b64.b64encode(b"<encrypted secret>").decode(),
                "_fieldOrder": "agrp,svce,acct", "_fieldTypes": "string,string,string",
                "_version": 3, "_length": 32,
            }
        demo = {
            "General": [
                mk("TEAMID01.com.example.notes", "com.example.notes", "user@example.com"),
                mk("TEAMID01.com.example.notes", "com.example.notes.refresh", "session-token"),
                mk("9ABCDE23.com.acme.rideshare", "com.acme.rideshare", "default_account"),
                mk("9ABCDE23.com.acme.rideshare", "com.acme.rideshare.sso", "sso-group-key"),
                mk("9ABCDE23.com.acme.rideshare", "Authentication.FormStore", "lastLoginIdentifier"),
                mk("KLM456PQ.com.sample.bank", "com.sample.bank.pin", "device-pin"),
            ],
            "Internet": [
                mk("apple", "WiFi", "HomeNetwork-5G"),
            ],
            "Certs": [],
            "Keys": [
                mk("KLM456PQ.com.sample.bank", "private-key", "rsa-2048"),
            ],
        }
        self.original_dump = {g: demo.get(g, []) for g in JSON_TO_PLIST}
        self.deleted.clear()
        self.edits.clear()
        self._index_records()
        # Mark a group deleted + one item edited so the colors show in the shot.
        for it in self.records:
            if as_text(it["rec"].get("agrp")) == "9ABCDE23.com.acme.rideshare":
                self.deleted.add(it["ref"])
        self.refresh_tree()
        self.set_status("DEMO DATA (synthetic) — marked 3 rideshare items for deletion.")
        if os.environ.get("KBE_DEMO") == "edit" and self.records:
            self.root.after(400, lambda: self._open_edit_dialog(self.records[0]))

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Backup folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.backup_dir, width=70).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse…", command=self.pick_backup).grid(row=0, column=2)

        ttk.Label(top, text="Password:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(top, textvariable=self.password, show="*", width=40).grid(row=1, column=1, sticky="w", padx=4)
        self.load_btn = ttk.Button(top, text="Load keychain", command=self.load_backup)
        self.load_btn.grid(row=1, column=2)

        ttk.Label(top, text="Runs folder:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(top, textvariable=self.out_base, width=70).grid(row=2, column=1, sticky="we", padx=4, pady=(4, 0))
        ttk.Button(top, text="Browse…", command=self.pick_out_base).grid(row=2, column=2, pady=(4, 0))

        ttk.Label(top, text="Run name:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(top, textvariable=self.run_name, width=40).grid(row=3, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(top, text="(optional label for this run's folder)").grid(row=3, column=2, sticky="w")

        top.columnconfigure(1, weight=1)

        # Search + actions
        bar = ttk.Frame(self.root, padding=(8, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="Search:").pack(side="left")
        e = ttk.Entry(bar, textvariable=self.search, width=40)
        e.pack(side="left", padx=4)
        e.bind("<KeyRelease>", lambda _e: self.refresh_tree())
        ttk.Button(bar, text="Delete selected", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Delete access group…", command=self.delete_group).pack(side="left", padx=4)
        ttk.Button(bar, text="Edit value…", command=self.edit_value).pack(side="left", padx=4)
        ttk.Button(bar, text="Edit JSON (side)", command=self.edit_json_builtin).pack(side="left", padx=4)
        ttk.Button(bar, text="Open in editor", command=self.edit_json_external).pack(side="left", padx=4)
        ttk.Button(bar, text="Undo marks", command=self.undo_marks).pack(side="left", padx=4)
        self.build_btn = ttk.Button(bar, text="Build backup", command=self.build_backup)
        self.build_btn.pack(side="right", padx=4)

        # Table
        mid = ttk.Frame(self.root, padding=8)
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", selectmode="extended")
        widths = {"group": 80, "agrp": 320, "svce": 320, "acct": 320, "class": 60}
        for c in COLUMNS:
            self.tree.heading(c, text=c, command=lambda col=c: self.sort_by(col))
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.tag_configure("deleted", background="#ffd6d6")
        self.tree.tag_configure("edited", background="#fff4c2")
        ysb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _e: self.edit_value())

        # Status + log
        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(fill="x", pady=(2, 4))
        self.log = tk.Text(bottom, height=8, wrap="word")
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

        self._sort_col = None
        self._sort_rev = False

    # ---------- logging ----------
    def logln(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def set_status(self, text):
        self.status.set(text)

    def status_async(self, text):
        """Update status safely from a worker thread."""
        self.root.after(0, lambda: self.set_status(text))

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.load_btn, self.build_btn):
            b.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ---------- irestore ----------
    def run_irestore(self, args):
        """Run one irestore command, stream output, capture the derived key."""
        env = dict(os.environ)
        env["IRESTORE_PASSWORD"] = self.env_password
        cmd = [str(IRESTORE)] + [str(a) for a in args]
        self.logln("RUN: " + " ".join(cmd[1:]))
        # Hide the console window irestore would otherwise flash on screen.
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
        proc = subprocess.Popen(
            cmd, cwd=str(APP_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            creationflags=creationflags, startupinfo=startupinfo,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                self.logln("   " + line)
            m = DERIVED_KEY_RE.search(line)
            if m:
                # Use the fast hex key for all later commands.
                self.env_password = m.group(1)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"irestore {args[1] if len(args) > 1 else ''} failed (exit {proc.returncode})")

    # ---------- actions ----------
    def pick_backup(self):
        d = filedialog.askdirectory(title="Select the backup folder (contains Manifest.db)")
        if d:
            self.backup_dir.set(d)

    def pick_out_base(self):
        d = filedialog.askdirectory(title="Select the folder to keep all runs in")
        if d:
            self.out_base.set(d)

    def _precheck(self):
        if self.busy:
            return False
        if not IRESTORE.exists():
            messagebox.showerror("Missing tool", f"irestore.exe not found next to this script:\n{IRESTORE}")
            return False
        b = self.backup_dir.get().strip()
        if not b or not (Path(b) / "Manifest.db").exists():
            messagebox.showerror("Backup", "Pick a backup folder that contains Manifest.db.")
            return False
        if not self.password.get():
            messagebox.showerror("Password", "Enter the backup password.")
            return False
        return True

    def load_backup(self):
        if not self._precheck():
            return
        self.env_password = self.password.get()
        base = Path(self.out_base.get().strip() or (APP_DIR / "KeychainEditor-Runs"))
        ts = time.strftime("%y%m%d-%H%M%S")
        label = re.sub(r"[^A-Za-z0-9._-]+", "-", self.run_name.get().strip()).strip("-")
        self.work_dir = base / (ts + ("-" + label if label else ""))
        self.set_busy(True)
        self.set_status("Loading keychain…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self):
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            backup = self.backup_dir.get().strip()
            dump = self.work_dir / "dump.json"
            orig = self.work_dir / "original-restore"
            self.status_async("Step 1/2: reading keychain (dumpkeys)…")
            self.run_irestore([backup, "dumpkeys", dump])
            self.status_async("Step 2/2: restoring original keychain…")
            self.run_irestore([backup, "restore", "KeychainDomain", orig])
            with open(dump, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.original_dump = {g: (data.get(g) or []) for g in JSON_TO_PLIST}
            self.deleted.clear()
            self.edits.clear()
            self._index_records()
            self.root.after(0, self.refresh_tree)
            n = sum(len(v) for v in self.original_dump.values())
            self.root.after(0, lambda: self.set_status(
                f"Loaded {n} readable items. (Items the tool cannot decrypt are kept automatically.)"))
        except Exception as e:
            self.logln("ERROR: " + str(e))
            self.root.after(0, lambda: messagebox.showerror("Load failed", str(e)))
            self.root.after(0, lambda: self.set_status("Load failed."))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def _restore_instructions(self, out_dir):
        """Step-by-step restore text with the real MobileSync paths."""
        device_id = Path(self.backup_dir.get().strip()).name
        home = Path.home()
        mobilesync = home / "Apple" / "MobileSync" / "Backup"
        appdata_alt = home / "AppData" / "Roaming" / "Apple Computer" / "MobileSync" / "Backup"
        return "\n".join([
            "HOW TO RESTORE THIS BACKUP",
            "=" * 40,
            "",
            "1. Close iTunes / the Apple Devices app completely.",
            "",
            "2. BACK UP YOUR ORIGINAL FIRST. Go to:",
            f"      {mobilesync}",
            f"   (older iTunes may use: {appdata_alt})",
            f"   Rename the existing folder  {device_id}",
            f"   to something like            {device_id} - ORIGINAL",
            "   so you keep a working copy.",
            "",
            "3. Copy the rebuilt backup into that MobileSync\\Backup folder and",
            f"   name it EXACTLY:  {device_id}",
            f"   Rebuilt backup is here: {out_dir}",
            "",
            "4. Open iTunes / Apple Devices, select your device,",
            "   choose 'Restore Backup…', pick this backup, and restore.",
            "   (If the backup is encrypted, you'll need the backup password.)",
            "",
            "5. After the phone restarts, reinstall the app from the App Store.",
            "",
            "If anything goes wrong, delete the edited folder and rename",
            f"'{device_id} - ORIGINAL' back to '{device_id}'.",
        ])

    def _write_run_summary(self, out_dir, deleted_count, edited_count):
        """Write a human-readable record of this run into its folder."""
        def fields(ref):
            for recs in self.original_dump.values():
                for r in recs:
                    if r.get("_ref") == ref:
                        return {"agrp": as_text(r.get("agrp")),
                                "svce": as_text(r.get("svce")),
                                "acct": as_text(r.get("acct"))}
            return {"agrp": "", "svce": "", "acct": ""}

        summary = {
            "run_name": self.run_name.get().strip(),
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_backup": self.backup_dir.get().strip(),
            "rebuilt_backup": str(out_dir),
            "deleted_count": deleted_count,
            "edited_count": edited_count,
            "deleted_items": [fields(r) for r in sorted(self.deleted)],
            "edited_items": [fields(r) for r in sorted(self.edits)],
        }
        with open(self.work_dir / "run-summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        lines = [
            "Keychain Backup Editor - run summary",
            "=" * 40,
            f"Run name      : {summary['run_name'] or '(none)'}",
            f"When          : {summary['when']}",
            f"Source backup : {summary['source_backup']}",
            f"Rebuilt backup: {summary['rebuilt_backup']}",
            f"Deleted       : {deleted_count}    Edited: {edited_count}",
            "",
            "Deleted items (access group | service | account):",
        ]
        for it in summary["deleted_items"]:
            lines.append(f"  - {it['agrp']} | {it['svce']} | {it['acct']}")
        if summary["edited_items"]:
            lines.append("")
            lines.append("Edited items:")
            for it in summary["edited_items"]:
                lines.append(f"  - {it['agrp']} | {it['svce']} | {it['acct']}")
        lines += ["", ""] + self._restore_instructions(out_dir).splitlines()
        (self.work_dir / "run-summary.txt").write_text("\n".join(lines), encoding="utf-8")
        (self.work_dir / "restore-instructions.txt").write_text(
            self._restore_instructions(out_dir), encoding="utf-8")
        self.logln("Wrote run-summary.txt / run-summary.json / restore-instructions.txt")

    @staticmethod
    def _json_default(o):
        if isinstance(o, (bytes, bytearray)):
            return base64.b64encode(bytes(o)).decode()
        raise TypeError(f"not JSON serializable: {type(o)}")

    def _index_records(self):
        self.records = []
        for group, recs in self.original_dump.items():
            for rec in recs:
                ref = rec.get("_ref")
                if ref is None:
                    continue
                self.records.append({"group": group, "ref": ref, "rec": rec})

    def _row_values(self, item):
        rec = self.edits.get(item["ref"], item["rec"])
        return (
            item["group"],
            as_text(rec.get("agrp")),
            as_text(rec.get("svce")),
            as_text(rec.get("acct")),
            as_text(rec.get("_class")),
        )

    def refresh_tree(self):
        q = self.search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for idx, item in enumerate(self.records):
            vals = self._row_values(item)
            if q and q not in " ".join(vals).lower():
                continue
            tags = []
            if item["ref"] in self.deleted:
                tags.append("deleted")
            elif item["ref"] in self.edits:
                tags.append("edited")
            self.tree.insert("", "end", iid=str(idx), values=vals, tags=tags)
        self._update_counts()

    def _update_counts(self):
        self.set_status(
            f"Marked for delete: {len(self.deleted)}   edited: {len(self.edits)}   "
            f"(total readable items: {len(self.records)})")

    def _selected_items(self):
        return [self.records[int(i)] for i in self.tree.selection()]

    def delete_selected(self):
        sel = self._selected_items()
        if not sel:
            return
        for it in sel:
            self.deleted.add(it["ref"])
        self.refresh_tree()

    def delete_group(self):
        groups = sorted({as_text(it["rec"].get("agrp")) for it in self.records if it["rec"].get("agrp")})
        agrp = simpledialog.askstring(
            "Delete access group",
            "Access group to delete (all its items will be removed):\n\n"
            + "\n".join(groups[:40]),
            parent=self.root)
        if not agrp:
            return
        count = 0
        for it in self.records:
            if as_text(it["rec"].get("agrp")) == agrp:
                self.deleted.add(it["ref"])
                count += 1
        self.logln(f"Marked {count} items in access group {agrp} for deletion.")
        self.refresh_tree()

    # Fields that are binary/structural — editing them would corrupt the item,
    # so they are shown read-only in the editor.
    READONLY_FIELDS = {
        "v_Data", "sha1", "klbl", "persistref", "UUID", "_ref", "_wkey",
        "_class", "_version", "_length", "_fieldOrder", "_fieldTypes",
    }

    def edit_value(self):
        sel = self._selected_items()
        if len(sel) != 1:
            messagebox.showinfo("Edit value", "Select exactly one row to edit.")
            return
        self._open_edit_dialog(sel[0])

    def _open_edit_dialog(self, item):
        rec = dict(self.edits.get(item["ref"], item["rec"]))

        win = tk.Toplevel(self.root)
        win.title("Edit item")
        win.transient(self.root)
        win.grab_set()
        win.geometry("640x560")

        ttk.Label(
            win,
            text=("Edit the values below, then Save. Greyed fields are binary/structural "
                  "and can't be changed.\nNote: deleting is the reliable operation — an "
                  "edited item is re-encrypted and\nmay not always be accepted by iOS."),
            foreground="#444", padding=8, justify="left").pack(fill="x")

        # Scrollable form
        outer = ttk.Frame(win, padding=(8, 0))
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        form = ttk.Frame(canvas)
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        entries = {}
        row = 0
        for key in sorted(rec.keys(), key=lambda k: (k.startswith("_"), k)):
            val = rec[key]
            ttk.Label(form, text=key, width=16, anchor="w").grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=as_text(val))
            ent = ttk.Entry(form, textvariable=var, width=60)
            editable = key not in self.READONLY_FIELDS and not isinstance(val, (list, dict))
            if not editable:
                ent.configure(state="readonly")
            ent.grid(row=row, column=1, sticky="we", pady=2, padx=4)
            entries[key] = (var, val, editable)
            row += 1
        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(win, padding=8)
        btns.pack(fill="x")

        def save():
            new_rec = dict(item["rec"])
            changed = False
            for key, (var, orig_val, editable) in entries.items():
                if not editable:
                    continue
                new_text = var.get()
                if new_text != as_text(orig_val):
                    # Preserve int type for numeric fields.
                    if isinstance(orig_val, bool):
                        new_rec[key] = new_text.strip().lower() in ("1", "true", "yes")
                    elif isinstance(orig_val, int):
                        try:
                            new_rec[key] = int(new_text)
                        except ValueError:
                            messagebox.showerror("Edit", f"{key} must be a whole number.", parent=win)
                            return
                    else:
                        new_rec[key] = new_text
                    changed = True
            if changed:
                self.edits[item["ref"]] = new_rec
                self.deleted.discard(item["ref"])
            else:
                self.edits.pop(item["ref"], None)
            self.refresh_tree()
            win.destroy()

        ttk.Button(btns, text="Save", command=save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)

    def undo_marks(self):
        self.deleted.clear()
        self.edits.clear()
        self.refresh_tree()

    # ---------- raw JSON editing ----------
    def _current_edited_dump(self):
        """The dump as it stands now (deletions removed, edits applied)."""
        out = {g: [] for g in JSON_TO_PLIST}
        for group, recs in self.original_dump.items():
            for rec in recs:
                ref = rec.get("_ref")
                if ref in self.deleted:
                    continue
                out[group].append(self.edits.get(ref, rec))
        return out

    def _apply_edited_dump(self, data):
        """Diff an edited dump against the pristine one and update marks."""
        if not isinstance(data, dict):
            raise ValueError("Top level must be a JSON object with "
                             "General/Internet/Certs/Keys arrays.")
        orig = {}
        for recs in self.original_dump.values():
            for rec in recs:
                orig[rec.get("_ref")] = rec
        seen, edits, unknown = set(), {}, 0
        for group in JSON_TO_PLIST:
            for rec in data.get(group) or []:
                ref = rec.get("_ref")
                if ref not in orig:
                    unknown += 1
                    continue
                seen.add(ref)
                if rec != orig[ref]:
                    edits[ref] = rec
        self.deleted = set(orig) - seen
        self.edits = edits
        self.refresh_tree()
        msg = f"Applied JSON: {len(self.deleted)} deleted, {len(self.edits)} edited."
        if unknown:
            msg += f"  Ignored {unknown} added/unknown item(s) — items can't be added."
        self.set_status(msg)

    def edit_json_builtin(self):
        if self.original_dump is None:
            messagebox.showinfo("Edit JSON", "Load a backup first.")
            return
        text = json.dumps(self._current_edited_dump(), indent=2)

        win = tk.Toplevel(self.root)
        win.title("Edit keychain JSON")
        win.transient(self.root)
        win.geometry("900x640")
        ttk.Label(win, padding=8, foreground="#444", justify="left",
                  text=("Edit the JSON, then Apply. Delete whole { … } items to remove them; "
                        "change values to edit.\nDon't touch _ref (items are matched by it). "
                        "Adding new items is ignored.")).pack(fill="x")
        txt = tk.Text(win, wrap="none", undo=True)
        ysb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ysb.set)
        txt.insert("1.0", text)
        ysb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        btns = ttk.Frame(win, padding=8)
        btns.pack(side="bottom", fill="x")

        def apply():
            try:
                data = json.loads(txt.get("1.0", "end"))
            except json.JSONDecodeError as e:
                messagebox.showerror("Invalid JSON", f"Line {e.lineno}: {e.msg}", parent=win)
                return
            try:
                self._apply_edited_dump(data)
            except Exception as e:
                messagebox.showerror("Edit JSON", str(e), parent=win)
                return
            win.destroy()

        ttk.Button(btns, text="Apply", command=apply).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)

    def edit_json_external(self):
        if self.original_dump is None:
            messagebox.showinfo("Edit JSON", "Load a backup first.")
            return
        self.work_dir.mkdir(parents=True, exist_ok=True)
        path = self.work_dir / "edit-me.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._current_edited_dump(), f, indent=2, default=self._json_default)
        try:
            os.startfile(str(path))  # opens the system default editor
        except Exception:
            subprocess.Popen(["notepad.exe", str(path)])
        if not messagebox.askokcancel(
                "External edit",
                "Opened in your editor:\n\n" + str(path) +
                "\n\nEdit and SAVE the file, then click OK to import it.\n"
                "(Delete whole items to remove them; keep _ref unchanged.)"):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            messagebox.showerror("Invalid JSON", f"Line {e.lineno}: {e.msg}")
            return
        self._apply_edited_dump(data)

    def sort_by(self, col):
        idx = COLUMNS.index(col)
        rev = self._sort_rev if self._sort_col == col else False
        self.records.sort(key=lambda it: self._row_values(it)[idx].lower())
        if rev:
            self.records.reverse()
        self._sort_col = col
        self._sort_rev = not rev
        self.refresh_tree()

    # ---------- build ----------
    def build_backup(self):
        if self.busy:
            return
        if self.original_dump is None:
            messagebox.showinfo("Build", "Load a backup first.")
            return
        if not self.deleted and not self.edits:
            messagebox.showinfo("Build", "No changes marked. Delete or edit something first.")
            return
        msg = (f"Create a new backup with:\n\n"
               f"  • {len(self.deleted)} item(s) deleted\n"
               f"  • {len(self.edits)} item(s) edited\n\n"
               f"The original backup is not modified.")
        if not messagebox.askokcancel("Build backup", msg):
            return
        self.set_busy(True)
        self.set_status("Building backup…")
        threading.Thread(target=self._build_worker, daemon=True).start()

    def _build_worker(self):
        try:
            backup = self.backup_dir.get().strip()
            orig_dir = self.work_dir / "original-restore"
            merge_dir = self.work_dir / "merged-restore"
            out_dir = self.work_dir / "rebuilt-backup"
            for d in (merge_dir, out_dir):
                if d.exists():
                    shutil.rmtree(d)

            orig_plist_path = orig_dir / "keychain-backup.plist"
            with open(orig_plist_path, "rb") as f:
                original = plistlib.load(f)
            original_bytes = orig_plist_path.read_bytes()
            fmt = plistlib.FMT_BINARY if original_bytes.startswith(b"bplist00") else plistlib.FMT_XML

            # Always save the edited dump JSON (deletions applied, edits applied)
            # so there is a readable record of exactly what was changed.
            self.status_async("Saving edited dump…")
            edited_json = self.work_dir / "edited-dump.json"
            edited = {g: [] for g in JSON_TO_PLIST}
            for group, recs in self.original_dump.items():
                for rec in recs:
                    ref = rec.get("_ref")
                    if ref in self.deleted:
                        continue
                    edited[group].append(self.edits.get(ref, rec))
            with open(edited_json, "w", encoding="utf-8") as f:
                json.dump(edited, f, indent=2, default=self._json_default)
            self.logln("Saved edited dump: " + str(edited_json))

            # If any values were edited, re-encrypt via encryptkeys to get new v_Data.
            generated_by_ref = {}
            if self.edits:
                self.status_async("Encrypting edited items…")
                gen_plist = self.work_dir / "generated.plist"
                self.run_irestore([backup, "encryptkeys", edited_json, gen_plist])
                with open(gen_plist, "rb") as f:
                    gen = plistlib.load(f)
                for plist_sec in JSON_TO_PLIST.values():
                    for r in gen.get(plist_sec, []) or []:
                        ref64 = base64.b64encode(bytes(r["v_PersistentRef"])).decode()
                        generated_by_ref[ref64] = r

            # Merge into the original restored plist: drop deleted, swap edited v_Data,
            # keep everything else byte-for-byte (including items the tool can't read).
            deleted_count = edited_count = 0
            for plist_sec in JSON_TO_PLIST.values():
                recs = original.get(plist_sec, []) or []
                new_recs = []
                for r in recs:
                    ref64 = base64.b64encode(bytes(r["v_PersistentRef"])).decode()
                    if ref64 in self.deleted:
                        deleted_count += 1
                        continue
                    if ref64 in self.edits and ref64 in generated_by_ref:
                        r = dict(r)
                        r["v_Data"] = generated_by_ref[ref64]["v_Data"]
                        edited_count += 1
                    new_recs.append(r)
                original[plist_sec] = new_recs

            self.status_async("Merging keychain…")
            shutil.copytree(orig_dir, merge_dir)
            with open(merge_dir / "keychain-backup.plist", "wb") as f:
                plistlib.dump(original, f, fmt=fmt, sort_keys=False)

            self.logln(f"Merged: deleted {deleted_count}, edited {edited_count}.")
            self.status_async("Rebuilding backup (this can take a minute)…")
            self.run_irestore([backup, "unrestore", "KeychainDomain", merge_dir, out_dir])

            self._write_run_summary(out_dir, deleted_count, edited_count)
            self.logln("DONE. Rebuilt backup at: " + str(out_dir))
            self.root.after(0, lambda: self.set_status("Backup built: " + str(out_dir)))
            instructions = self._restore_instructions(out_dir)
            self.root.after(0, lambda: messagebox.showinfo(
                "Backup ready — read the restore steps",
                "Run folder (dump, edited dump, summary, instructions):\n"
                + str(self.work_dir) + "\n\n" + instructions))
        except Exception as e:
            self.logln("ERROR: " + str(e))
            self.root.after(0, lambda: messagebox.showerror("Build failed", str(e)))
            self.root.after(0, lambda: self.set_status("Build failed."))
        finally:
            self.root.after(0, lambda: self.set_busy(False))


def main():
    root = tk.Tk()
    EditorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
