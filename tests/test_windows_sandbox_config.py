import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.windows_sandbox_config import TARGETS, build_config


class WindowsSandboxConfigTests(unittest.TestCase):
    def test_network_disabled_and_only_data_writable(self):
        paths = {name: "C:\\host\\" + name for name in TARGETS}
        paths["documents"] = "C:\\host\\docs & notes"
        root = ET.fromstring(build_config(paths))
        self.assertEqual(root.findtext("Networking"), "Disable")
        self.assertEqual(root.findtext("ClipboardRedirection"), "Disable")
        self.assertEqual(root.findtext("AudioInput"), "Disable")
        folders = root.findall("./MappedFolders/MappedFolder")
        self.assertEqual(len(folders), 6)
        for (name, (target, readonly)), folder in zip(TARGETS.items(), folders, strict=True):
            self.assertEqual(folder.findtext("HostFolder"), paths[name])
            self.assertEqual(folder.findtext("SandboxFolder"), target)
            self.assertEqual(folder.findtext("ReadOnly"), str(readonly).lower())
        self.assertIn("windows_sandbox_start.cmd", root.findtext("./LogonCommand/Command"))
        self.assertEqual(ET.fromstring(build_config(paths, audio=True)).findtext("AudioInput"), "Enable")

    def test_incomplete_mappings_are_rejected(self):
        with self.assertRaises(ValueError):
            build_config({"project": "C:\\project"})

    def test_start_script_checks_offline_config_and_never_installs(self):
        script = (Path(__file__).resolve().parent.parent / "scripts/windows_sandbox_start.cmd").read_text()
        self.assertIn("not c.ollama.auto_pull", script)
        self.assertIn("OLLAMA_MODELS=C:\\hinata\\models", script)
        self.assertNotIn("pip install", script.lower())
        self.assertNotIn("ollama.exe pull", script.lower())


if __name__ == "__main__":
    unittest.main()
