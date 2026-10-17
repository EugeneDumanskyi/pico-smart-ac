# SPDX-License-Identifier: GPL-3.0-only
# FileStore on the Unix port: in a temporary directory (VfsPosix), and on littlefs in RAM, the Pico's file system.
import json
import os
import unittest

from smartac.shell.store import EVENT_LOG_BYTES, MAX_CRASH, FileStore

MOUNT = "/smartac_test_lfs"


class RAMBlockDev:
    # The block device protocol os.VfsLfs2 needs (MicroPython docs, "Filesystems").
    def __init__(self, block_size=512, blocks=128):
        self.block_size = block_size
        self.data = bytearray(block_size * blocks)

    def readblocks(self, n, buf, offset=0):
        addr = n * self.block_size + offset
        buf[:] = self.data[addr : addr + len(buf)]

    def writeblocks(self, n, buf, offset=None):
        if offset is None:
            self.ioctl(6, n)
            offset = 0
        addr = n * self.block_size + offset
        self.data[addr : addr + len(buf)] = buf

    def ioctl(self, op, arg):
        if op == 4:  # block count
            return len(self.data) // self.block_size
        if op == 5:  # block size
            return self.block_size
        if op == 6:  # erase
            self.data[arg * self.block_size : (arg + 1) * self.block_size] = b"\xff" * self.block_size
            return 0
        return None


def _rmtree(path):
    for name, kind, *_ in os.ilistdir(path):
        p = path + "/" + name
        if kind == 0x4000:
            _rmtree(p)
        else:
            os.remove(p)
    os.rmdir(path)


def _read(path):
    with open(path) as f:
        return f.read()


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


class Cases:
    # Mixed into one TestCase per file system; setUp makes self.root.
    def test_save_and_load(self):
        s = FileStore(self.root)
        self.assertIsNone(s.load("state"))
        s.save("state", {"a": [1, None]})
        self.assertEqual(s.load("state"), {"a": [1, None]})
        self.assertEqual(_read(self.root + "/state.json"), '{"a": [1, null]}')
        self.assertEqual(sorted(os.listdir(self.root)), ["state.json"])

    def test_previous_version_is_kept_as_bak(self):
        s = FileStore(self.root)
        s.save("settings", {"v": 1})
        s.save("settings", {"v": 2})
        self.assertEqual(json.loads(_read(self.root + "/settings.json.bak")), {"v": 1})
        self.assertEqual(sorted(os.listdir(self.root)), ["settings.json", "settings.json.bak"])

    def test_load_falls_back_to_bak(self):
        s = FileStore(self.root)
        s.save("settings", {"v": 1})
        s.save("settings", {"v": 2})
        _write(self.root + "/settings.json", "{broken")
        self.assertEqual(s.load("settings"), {"v": 1})
        os.remove(self.root + "/settings.json")  # a power cut between the two renames
        self.assertEqual(s.load("settings"), {"v": 1})
        os.remove(self.root + "/settings.json.bak")
        self.assertIsNone(s.load("settings"))

    def test_leftover_tmp_is_ignored_and_replaced(self):
        s = FileStore(self.root)
        s.save("state", {"v": 1})
        _write(self.root + "/state.json.tmp", "{half a wri")  # a power cut while writing
        self.assertEqual(s.load("state"), {"v": 1})
        s.save("state", {"v": 2})
        self.assertEqual(s.load("state"), {"v": 2})
        self.assertFalse("state.json.tmp" in os.listdir(self.root))

    def test_event_log_rotates(self):
        s = FileStore(self.root)
        per_file = EVENT_LOG_BYTES // 100  # 100-byte lines, newline included
        for i in range(per_file):
            s.append_event("%099d" % i)
        self.assertEqual(sorted(os.listdir(self.root)), ["events.log"])
        self.assertEqual(os.stat(self.root + "/events.log")[6], per_file * 100)
        s.append_event("%099d" % per_file)  # doesn't fit any more
        self.assertEqual(_read(self.root + "/events.log"), "%099d\n" % per_file)
        old = _read(self.root + "/events.1.log").splitlines()
        self.assertEqual((len(old), int(old[0]), int(old[-1])), (per_file, 0, per_file - 1))
        for i in range(per_file):
            s.append_event("%099d" % (per_file + 1 + i))
        old = _read(self.root + "/events.1.log").splitlines()
        self.assertEqual(int(old[0]), per_file)  # the oldest file was dropped

    def test_boot_counter(self):
        s = FileStore(self.root)
        self.assertEqual([s.next_boot(), s.next_boot()], [1, 2])
        self.assertEqual(FileStore(self.root).next_boot(), 3)

    def test_crash(self):
        s = FileStore(self.root)
        self.assertIsNone(s.load_crash())
        s.save_crash("Traceback\nValueError: x\n")
        self.assertEqual(FileStore(self.root).load_crash(), "Traceback\nValueError: x\n")
        s.save_crash("y" * (MAX_CRASH + 10))
        self.assertEqual(len(s.load_crash()), MAX_CRASH)

    def test_reset_counts(self):
        s = FileStore(self.root)
        self.assertEqual(s.reset_counts("power_on"), {"power_on": 1})
        s.reset_counts("watchdog")
        self.assertEqual(FileStore(self.root).reset_counts("power_on"), {"power_on": 2, "watchdog": 1})
        _write(self.root + "/resets.json", '{"crash": "x", "watchdog": 3}')
        os.remove(self.root + "/resets.json.bak")
        self.assertEqual(s.reset_counts("crash"), {"crash": 1, "watchdog": 3})

    def test_root_with_trailing_slash(self):
        FileStore(self.root + "/").save("boot", {"count": 1})
        self.assertEqual(FileStore(self.root).load("boot"), {"count": 1})


class TestPosix(Cases, unittest.TestCase):
    def setUp(self):
        base = (os.getenv("TMPDIR") or "/tmp").rstrip("/")
        self.root = "%s/smartac-test-%s" % (base, "".join("%02x" % b for b in os.urandom(4)))
        os.mkdir(self.root)

    def tearDown(self):
        _rmtree(self.root)


class TestLittlefs(Cases, unittest.TestCase):
    def setUp(self):
        bdev = RAMBlockDev()
        os.VfsLfs2.mkfs(bdev)
        os.mount(os.VfsLfs2(bdev), MOUNT)
        self.root = MOUNT

    def tearDown(self):
        os.umount(MOUNT)
