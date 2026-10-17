"""Regression coverage for project-owned protected path configuration."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import runner_config as config


class ProtectedPathsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save(self, paths):
        (self.root / config.CONFIG_FILE).write_text(json.dumps({
            "checks": [["true"]], "runners": {"worker": ["true"]}, "protected_paths": paths,
        }))

    def test_normalized_directory_entries_protect_files_and_keep_boundaries(self):
        for path in ("private", "./private", "private/", "private//", "./private/./"):
            with self.subTest(path=path):
                self.save([path])
                protected = config.protected_paths(self.root)
                self.assertTrue(config.is_protected("private", protected))
                self.assertTrue(config.is_protected("private/data.txt", protected))
                self.assertFalse(config.is_protected("private-backup/data.txt", protected))

    def test_absolute_traversal_and_empty_entries_are_rejected(self):
        invalid = ("../private", "private/../other", "/private", "//private", "C:/private",
                   r"C:\private", r"private\data", "", " ", ".", "./", "private\x00data", 1, None)
        for path in invalid:
            with self.subTest(path=path):
                self.save([path])
                with self.assertRaises(config.RunnerError):
                    config.protected_paths(self.root)
                with self.assertRaises(config.RunnerError):
                    config.load_config(self.root)

    def test_invalid_container_is_rejected(self):
        for paths in (None, "private", {"private": True}):
            with self.subTest(paths=paths):
                self.save(paths)
                with self.assertRaises(config.RunnerError):
                    config.protected_paths(self.root)

    def test_configuration_keeps_builtin_protection(self):
        self.save(["private"])
        protected = config.protected_paths(self.root)
        for path in (".git/config", "tasks/tasks.db", config.CONFIG_FILE, "private/data.txt"):
            with self.subTest(path=path):
                self.assertTrue(config.is_protected(path, protected))

    def test_project_relative_dot_prefix_cannot_bypass_protection(self):
        self.save(["private"])
        self.assertTrue(config.is_protected("./private/data.txt", config.protected_paths(self.root)))
