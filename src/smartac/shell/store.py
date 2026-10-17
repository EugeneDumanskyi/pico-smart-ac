# SPDX-License-Identifier: GPL-3.0-only
# Persistence (architecture.md, "Persistence and flash"). MemoryStore for the host runner and tests, FileStore for
# the Pico's flash; both keep this API:
#   load(name) -> parsed document or None, save(name, doc), append_event(line), next_boot() -> int,
#   load_crash() -> text or None, save_crash(text), reset_counts(cause) -> {cause: count}
# Documents: "profiles", "settings", "state" (the controller snapshot), "boot" (the boot counter),
# "resets" (resets by cause).
import json
import os

MAX_EVENT_LINES = 400  # about what the 2 x 8 KB rotating log holds
EVENT_LOG_BYTES = 8 * 1024
MAX_CRASH = 2048


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


class _Store:
    def next_boot(self):
        # Count this boot and return its number (1 on a fresh store).
        doc = self.load("boot")
        n = _count(doc.get("count") if isinstance(doc, dict) else None) + 1
        self.save("boot", {"count": n})
        return n

    def reset_counts(self, cause):
        # Count this boot's reset cause; call once per boot. -> every cause seen so far with its count.
        doc = self.load("resets")
        counts = {}
        if isinstance(doc, dict):
            for k, v in doc.items():
                if _count(v):
                    counts[k] = v
        counts[cause] = counts.get(cause, 0) + 1
        self.save("resets", counts)
        return counts


class MemoryStore(_Store):
    # Keeps each document as the JSON text a file would hold, so a simulated reboot reads back exactly that.
    def __init__(self):
        self.docs = {}
        self.events = []
        self.crash = None

    def load(self, name):
        text = self.docs.get(name)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def save(self, name, doc):
        self.docs[name] = json.dumps(doc)

    def append_event(self, line):
        self.events.append(line)
        if len(self.events) > MAX_EVENT_LINES:
            self.events.pop(0)

    def load_crash(self):
        return self.crash

    def save_crash(self, text):
        self.crash = text[:MAX_CRASH]


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _read(path):
    with open(path) as f:
        return f.read()


class FileStore(_Store):
    # JSON files under root. A save writes name.json.tmp, moves name.json to name.json.bak, then the .tmp into
    # place, so a power cut leaves either file whole; load falls back to the .bak.
    # events.log rotates to events.1.log at EVENT_LOG_BYTES. crash.txt holds the last crash only.
    def __init__(self, root):
        self.root = root.rstrip("/") + "/"

    def _path(self, name):
        return self.root + name + ".json"

    def load(self, name):
        path = self._path(name)
        for p in (path, path + ".bak"):
            try:
                return json.loads(_read(p))
            except (OSError, ValueError):
                pass
        return None

    def save(self, name, doc):
        path = self._path(name)
        with open(path + ".tmp", "w") as f:
            f.write(json.dumps(doc))
        if _exists(path):
            os.rename(path, path + ".bak")
        os.rename(path + ".tmp", path)

    def append_event(self, line):
        path = self.root + "events.log"
        try:
            size = os.stat(path)[6]
        except OSError:
            size = 0
        if size and size + len(line) + 1 > EVENT_LOG_BYTES:
            os.rename(path, self.root + "events.1.log")
        with open(path, "a") as f:
            f.write(line + "\n")

    def load_crash(self):
        try:
            return _read(self.root + "crash.txt")
        except OSError:
            return None

    def save_crash(self, text):
        with open(self.root + "crash.txt", "w") as f:
            f.write(text[:MAX_CRASH])
