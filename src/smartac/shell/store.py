# SPDX-License-Identifier: GPL-3.0-only
# Persistence (architecture.md, "Persistence and flash"). For now only MemoryStore, for the host runner and tests;
# the file store (atomic .tmp -> rename, .bak, event log rotation) comes with the Pico wiring and keeps this API:
#   load(name) -> parsed document or None, save(name, doc), append_event(line), next_boot() -> int
# Documents: "profiles", "settings", "state" (the controller snapshot), "boot" (the boot counter).
import json

MAX_EVENT_LINES = 400  # about what the 2 x 8 KB rotating log holds


class MemoryStore:
    # Keeps each document as the JSON text a file would hold, so a simulated reboot reads back exactly that.
    def __init__(self):
        self.docs = {}
        self.events = []

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

    def next_boot(self):
        # Count this boot and return its number (1 on a fresh store).
        doc = self.load("boot")
        n = doc.get("count") if isinstance(doc, dict) else None
        n = (n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0) + 1
        self.save("boot", {"count": n})
        return n
