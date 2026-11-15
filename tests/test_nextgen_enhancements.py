"""Tests for Windows Job Object sandbox, SQLCipher driver support, ANN/FTS5 RAG,
hash-chained tamper-evident audit traces, procedural playbooks, and AST/multi-file patches.
"""

from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hinata.automation.file_manager import FileManager
from hinata.automation.workspace import WorkspaceManager, _WindowsJobSandbox
from hinata.config import SecurityConfig
from hinata.core.observability import TraceRecorder
from hinata.core.planner import Planner
from hinata.database.database import Database
from hinata.knowledge.index import KnowledgeIndex
from hinata.knowledge.vector_ann import LocalVectorIndex
from hinata.security.action_validator import ActionValidator
from hinata.security.local_protection import LocalVault, ProtectionError
from hinata.tasks.playbooks import PlaybookManager
from hinata.tasks.store import TaskStateError, TaskStore


class NextGenEnhancementsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "hinata.db"

    def test_windows_job_object_sandbox_lifecycle_and_limits(self) -> None:
        k32 = MagicMock()
        k32.CreateJobObjectW.return_value = 4242
        k32.SetInformationJobObject.return_value = 1
        k32.AssignProcessToJobObject.return_value = 1
        k32.TerminateJobObject.return_value = 1
        k32.CloseHandle.return_value = 1

        job = _WindowsJobSandbox(max_memory_mb=512, max_processes=8, kernel32=k32)
        handle = job.create_and_configure()
        self.assertEqual(handle, 4242)
        self.assertEqual(k32.SetInformationJobObject.call_count, 2)

        job.assign_process(9999)
        k32.AssignProcessToJobObject.assert_called_once()

        job.terminate(exit_code=137)
        k32.TerminateJobObject.assert_called_once()

        job.close()
        k32.CloseHandle.assert_called_once()

        # Verify CreateJobObjectW failure raises OSError cleanly
        bad_k32 = MagicMock()
        bad_k32.CreateJobObjectW.return_value = 0
        bad_job = _WindowsJobSandbox(kernel32=bad_k32)
        with self.assertRaises(OSError):
            bad_job.create_and_configure()

    def test_sqlcipher_driver_backend_selection_and_fail_closed(self) -> None:
        vault = LocalVault(self.root / "vault_dir", enabled=True)

        # Strict 'sqlcipher' fails closed when sqlcipher3 is not installed
        with patch("hinata.database.database._detect_sqlcipher_driver", return_value=None):
            with self.assertRaises(ProtectionError):
                Database(self.root / "strict_cipher.db", vault=vault, encryption_backend="sqlcipher")

            # 'auto' falls back cleanly to LocalVault envelope encryption
            auto_db = Database(self.root / "auto_cipher.db", vault=vault, encryption_backend="auto")
            self.assertEqual(auto_db.active_encryption_backend, "local_vault_envelope")

        # Mock SQLCipher DB-API driver: verify PRAGMA key & cipher_memory_security are issued
        pragma_calls: list[str] = []

        class _CipherConnWrapper:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self._inner = inner

            @property
            def row_factory(self):
                return self._inner.row_factory

            @row_factory.setter
            def row_factory(self, val) -> None:
                self._inner.row_factory = val

            def execute(self, sql: str, params=()):
                if sql.upper().startswith("PRAGMA KEY") or sql.upper().startswith("PRAGMA CIPHER_"):
                    pragma_calls.append(sql)
                    return self._inner.execute("SELECT 1")
                return self._inner.execute(sql, params)

            def executescript(self, script: str):
                return self._inner.executescript(script)

            def commit(self) -> None:
                self._inner.commit()

            def rollback(self) -> None:
                self._inner.rollback()

            def close(self) -> None:
                self._inner.close()

        fake_driver = MagicMock()
        fake_driver.connect.side_effect = lambda p, timeout=5.0: _CipherConnWrapper(
            sqlite3.connect(p, timeout=timeout)
        )

        with patch("hinata.database.database._detect_sqlcipher_driver", return_value=fake_driver):
            cipher_db = Database(
                self.root / "encrypted_page.db", vault=vault, encryption_backend="sqlcipher"
            )
            self.assertEqual(cipher_db.active_encryption_backend, "sqlcipher_page_and_envelope")
            self.assertTrue(any("PRAGMA key = \"x'" in s for s in pragma_calls))
            self.assertIn("PRAGMA cipher_memory_security = ON", pragma_calls)
            status = cipher_db.encryption_status()
            self.assertTrue(status["sqlcipher_driver_installed"])

    def test_vector_ann_simhash_lsh_scales_and_fts5_bm25_retrieves(self) -> None:
        # Test ANN mode above exact_threshold (e.g. threshold=64 with 400 vectors)
        dim = 16
        items: list[tuple[tuple[str, int], list[float]]] = []
        for i in range(400):
            angle = (i / 400.0) * math.pi * 2.0
            raw = [math.cos(angle + d * 0.3) for d in range(dim)]
            norm = math.sqrt(sum(x * x for x in raw))
            items.append(((f"/docs/doc_{i}.md", 1), [x / norm for x in raw]))

        target_key, target_vec = items[137]

        # 1. Test NumPy / HNSW auto backend
        ann = LocalVectorIndex(exact_threshold=64, num_tables=6, bits_per_table=10)
        backend = ann.build_or_reuse("embed-v1", items)
        self.assertIn(backend, {"numpy_simhash_lsh_ann", "hnswlib_ann", "stdlib_simhash_lsh_ann"})
        hits = ann.query(target_vec, top_k=5)
        self.assertEqual(hits[0].key, target_key)
        self.assertGreater(hits[0].score, 0.99)

        # 2. Test pure-Python stdlib SimHash LSH fallback explicitly
        with patch("hinata.knowledge.vector_ann._load_optional", return_value=None):
            py_ann = LocalVectorIndex(exact_threshold=64, num_tables=6, bits_per_table=10)
            py_backend = py_ann.build_or_reuse("embed-v1", items)
            self.assertEqual(py_backend, "stdlib_simhash_lsh_ann")
            py_hits = py_ann.query(target_vec, top_k=5)
            self.assertEqual(py_hits[0].key, target_key)
            self.assertGreater(py_hits[0].score, 0.99)

        # 3. Test SQLite FTS5 BM25 search inside KnowledgeIndex
        db = Database(self.db_path)
        files = FileManager(SecurityConfig(allowed_roots=[self.root]))
        doc = self.root / "architecture_notes.md"
        doc.write_text(
            "# Hyperplane Indexing\nLocality sensitive hashing accelerates large offline corpora.\n",
            encoding="utf-8",
        )
        kindex = KnowledgeIndex(db, files)
        kindex.index(str(doc))
        bm25_hits = kindex.bm25_fts_search("hyperplane locality hashing", limit=3)
        self.assertEqual(len(bm25_hits), 1)
        self.assertIn("Locality sensitive hashing", bm25_hits[0]["excerpt"])
        stats = kindex.index_stats()
        self.assertEqual(stats["documents"], 1)

    def test_tamper_evident_hash_chained_trace_ledger_detects_tampering(self) -> None:
        db = Database(self.db_path)
        tracer = TraceRecorder(db)
        run_id, trace_id = tracer.start_run(
            user_goal="Verify cryptographic audit ledger",
            source="text",
        )
        tracer.record_event(
            run_id=run_id,
            correlation_id=trace_id,
            agent_id="planner",
            event_type="plan_validated",
            status="OK",
            payload={"steps": 2},
        )
        tracer.update_run(run_id, state="COMPLETED", summary="Done")

        integrity = tracer.verify_integrity()
        self.assertTrue(integrity["intact"])
        self.assertGreaterEqual(integrity["checked_events"], 2)

        # Tamper with an existing row payload directly in SQLite -> verify_integrity catches it!
        db.execute(
            "UPDATE agent_trace_events SET payload_json = '{\"steps\": 999}' WHERE event_type = 'plan_validated'"
        )
        broken = tracer.verify_integrity()
        self.assertFalse(broken["intact"])
        self.assertIn("mismatch", broken["reason"])

    def test_procedural_playbooks_promote_parameterize_and_stage_approval(self) -> None:
        db = Database(self.db_path)
        validator = ActionValidator()
        planner = Planner(validator)
        tasks = TaskStore(db)
        playbooks = PlaybookManager(db, planner, tasks)

        # Create and complete a verified folder task
        req = f"ensure folder {self.root / 'proj_alpha'} exists"
        plan = planner.validate(
            {
                "type": "plan",
                "objective": "Create project workspace folder",
                "steps": [{"tool": "create_folder", "arguments": {"path": str(self.root / "proj_alpha")}}],
            },
            user_request=req,
        )
        tid = tasks.create(plan, "text", user_request=req)

        # Unverified AWAITING_APPROVAL task cannot be promoted
        with self.assertRaises(TaskStateError):
            playbooks.promote_from_task(tid, name="bootstrap_project")

        tasks.claim(tid, plan, "text")
        tasks.start_step(tid, 1)
        (self.root / "proj_alpha").mkdir()
        tasks.finish_step(tid, 1, True, "created")
        tasks.begin_verification(
            tid, [{"step": 1, "tool": "create_folder", "status": "verified", "detail": "exists"}]
        )
        tasks.complete_folder_objective(
            tid,
            {
                "kind": "folder_exists",
                "path": str(self.root / "proj_alpha"),
                "requested": req,
                "tool_path": str(self.root / "proj_alpha"),
            },
        )

        # Promote to parameterized playbook replacing 'proj_alpha' with '{{project_slug}}'
        promoted = playbooks.promote_from_task(
            tid,
            name="bootstrap-project",
            description="Bootstrap a named project folder",
            parameter_bindings={"project_slug": "proj_alpha"},
            tags=["onboarding", "folders"],
        )
        self.assertEqual(promoted["parameters"], ["project_slug"])

        # Instantiate for 'proj_beta' -> stages fresh AWAITING_APPROVAL task without executing!
        new_tid, new_plan = playbooks.instantiate(
            "bootstrap-project", params={"project_slug": "proj_beta"}
        )
        staged = tasks.get(new_tid)
        assert staged is not None
        self.assertEqual(staged["state"], "AWAITING_APPROVAL")
        self.assertIn("proj_beta", new_plan.preview())
        self.assertFalse((self.root / "proj_beta").exists())

        # Secret injection into playbook parameter is blocked
        with self.assertRaises(ValueError):
            playbooks.instantiate(
                "bootstrap-project",
                params={"project_slug": "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"},
            )

    def test_workspace_ast_symbol_index_and_atomic_multifile_patch(self) -> None:
        db = Database(self.db_path)
        db.ensure_migrated()
        ws_dir = self.root / "sample_repo"
        ws_dir.mkdir()
        (ws_dir / "math_ops.py").write_text(
            '"""Math utilities."""\n\ndef add(a: int, b: int) -> int:\n    return a + b\n',
            encoding="utf-8",
        )
        (ws_dir / "service.py").write_text(
            "from math_ops import add\n\nclass Calculator:\n    def compute(self, x: int) -> int:\n        return add(x, 1)\n",
            encoding="utf-8",
        )

        wm = WorkspaceManager(db, [self.root])
        wm.register_workspace(ws_dir, name="sample_repo")

        sym = wm.ast_symbol_index("sample_repo")
        names = {s["name"] for s in sym["symbols"]}
        self.assertIn("add", names)
        self.assertIn("Calculator", names)

        # Propose, apply, and revert an atomic multi-file patch across both files + a new file
        proposal = wm.propose_multifile_patch(
            workspace="sample_repo",
            title="Upgrade add and Calculator + add constants.py",
            changes=[
                {
                    "path": "constants.py",
                    "new_content": "OFFSET = 10\n",
                },
                {
                    "path": "service.py",
                    "new_content": "from constants import OFFSET\nfrom math_ops import add\n\nclass Calculator:\n    def compute(self, x: int) -> int:\n        return add(x, OFFSET)\n",
                },
            ],
        )
        self.assertEqual(proposal["file_count"], 2)

        applied = wm.apply_patch(proposal["patch_id"])
        self.assertEqual(applied["state"], "APPLIED")
        self.assertTrue((ws_dir / "constants.py").is_file())
        self.assertIn("OFFSET", (ws_dir / "service.py").read_text(encoding="utf-8"))

        reverted = wm.revert_patch(proposal["patch_id"])
        self.assertEqual(reverted["state"], "REVERTED")
        self.assertFalse((ws_dir / "constants.py").exists())
        self.assertNotIn("OFFSET", (ws_dir / "service.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
