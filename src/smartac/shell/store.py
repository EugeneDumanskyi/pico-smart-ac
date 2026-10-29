# SPDX-License-Identifier: GPL-3.0-only
# Persistence (architecture.md, "Persistence and flash"). MemoryStore for the host runner and tests, FileStore for
# the Pico's flash; both keep this API:
#   load(name) -> parsed document or None, save(name, doc), append_event(line) -> False if the flash refused it,
#   next_boot(save=True) -> int, load_crash() -> text or None, save_crash(text),
#   reset_counts(cause, save=True) -> {cause: count},
#   read_events() -> the event log as text pieces, oldest first (GET /api/log), tail_events(n) -> its last n lines
# save() and the counters raise OSError when the flash is full (save=False: the count without saving it).
# Documents: "profiles", "settings", "state" (the controller snapshot), "boot" (the boot counter),
# "resets" (resets by cause).
import json
import os

MAX_EVENT_LINES = 1300  # about what the 2 x 64 KB rotating log holds
# At least 64 KB kept: about 100 decision lines a day plus 24 hourly heap lines, so over 9 days.
EVENT_LOG_BYTES = 64 * 1024
READ_CHUNK = 512
LOGS = ("events.1.log", "events.log")  # oldest first
MAX_CRASH = 2048


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


class _Store:
    def next_boot(self, save=True):
        # Count this boot and return its number (1 on a fresh store).
        doc = self.load("boot")
        n = _count(doc.get("count") if isinstance(doc, dict) else None) + 1
        if save:
            self.save("boot", {"count": n})
        return n

    def reset_counts(self, cause, save=True):
        # Count this boot's reset cause; call once per boot. -> every cause seen so far with its count.
        doc = self.load("resets")
        counts = {}
        if isinstance(doc, dict):
            for k, v in doc.items():
                if _count(v):
                    counts[k] = v
        counts[cause] = counts.get(cause, 0) + 1
        if save:
            self.save("resets", counts)
        return counts


class MemoryStore(_Store):
    # Keeps each document as the JSON text a file would hold, so a simulated reboot reads back exactly that.
    def __init__(self):
        self.docs = {}
        self.events = []
        self.crash = None
        self.fail_append = False  # tests: a flash that refuses the log

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
        if self.fail_append:
            return False
        self.events.append(line)
        if len(self.events) > MAX_EVENT_LINES:
            self.events.pop(0)
        return True

    def read_events(self):
        for line in self.events:
            yield line + "\n"

    def tail_events(self, n):
        for line in self.events[-n:]:
            yield line + "\n"

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


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


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
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(doc, f)  # streamed: no copy of the whole document as one string
        except OSError:
            # Flash full: no half-written .tmp left behind, and name.json as it was.
            _remove(path + ".tmp")
            raise
        if _exists(path):
            os.rename(path, path + ".bak")
        os.rename(path + ".tmp", path)

    def append_event(self, line):
        # -> False if the flash refused the line (full): the log is a record, never a reason to reset.
        path = self.root + "events.log"
        try:
            size = os.stat(path)[6]
        except OSError:
            size = 0
        try:
            if size and size + len(line) + 1 > EVENT_LOG_BYTES:
                os.rename(path, self.root + "events.1.log")
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError:
            return False
        return True

    def read_events(self):
        return self._stream(LOGS[0], 0)

    def tail_events(self, n):
        # The last n lines (n >= 1) as read_events pieces. A backward scan finds where they start, in events.log or,
        # if that holds fewer, in events.1.log, so any n takes constant memory. A rotation between the scan and the
        # read starts it in the wrong place: the first line may be cut (from_line drops it).
        for name in (LOGS[1], LOGS[0]):
            pos, n = self._line_start(name, n)
            if pos is not None:
                return self._stream(name, pos)
        return self._stream(LOGS[0], 0)  # fewer than n lines in all

    def _line_start(self, name, n):
        # -> (the offset of the nth line from the end, 0), or (None, n less this file's lines). The file's last byte
        # ends its last line, newline or not; an empty or missing file has none.
        path = self.root + name
        try:
            size = os.stat(path)[6]
        except OSError:
            return None, n
        end = size - 1
        while end > 0:
            begin = max(0, end - READ_CHUNK)
            try:
                with open(path, "rb") as f:
                    f.seek(begin)
                    data = f.read(end - begin)
            except OSError:
                return None, n
            i = len(data)
            while True:
                i = data.rfind(b"\n", 0, i)
                if i < 0:
                    break
                n -= 1
                if n == 0:
                    return begin + i + 1, 0
            end = begin
        if size:
            n -= 1  # the first line, which no newline starts
            if n == 0:
                return 0, 0
        return None, n

    def _stream(self, first, pos):
        # From `first` at pos to the end of events.log, in READ_CHUNK pieces. Each piece opens and closes the file,
        # so none is held open while the HTTP task waits on the socket and the control task appends or rotates.
        # A rotation in between can drop or repeat lines in this read; the next one has them (shadow_pull.py
        # dedups). A file whose last line has no newline (a power cut) gets one, so it doesn't run into the next.
        for name in LOGS[LOGS.index(first):]:
            last = b"\n"
            while True:
                try:
                    with open(self.root + name, "rb") as f:
                        f.seek(pos)
                        data = f.read(READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                pos += len(data)
                last = data[-1:]
                yield data
            if last != b"\n":
                yield b"\n"
            pos = 0

    def load_crash(self):
        try:
            return _read(self.root + "crash.txt")
        except OSError:
            return None

    def save_crash(self, text):
        with open(self.root + "crash.txt", "w") as f:
            f.write(text[:MAX_CRASH])
