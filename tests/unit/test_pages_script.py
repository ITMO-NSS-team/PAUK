"""The public publisher must refuse private data before any GitHub command."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PagesPublisherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        public = self.root / "public"
        public.mkdir()
        (public / "graph-data.json").write_text(json.dumps({"authors": [{"key": "a0", "label": "Ив… И."}]}))
        for name in ("authors", "repos", "pubs"):
            (public / f"{name}-detail.json").write_text("[]")
        (public / "private.json").write_text("MUST NOT BE UPLOADED")
        binary = self.root / "bin"
        binary.mkdir()
        fake = binary / "gh"
        fake.write_text("""#!/usr/bin/env python3
import json, os, sys, tarfile
from pathlib import Path
root = Path(os.environ['PUBLISH_TEST_ROOT'])
args = sys.argv[1:]
with (root/'calls').open('a') as stream:
    stream.write(json.dumps(args)+'\\n')
if args[:2] == ['api', 'user']:
    print('test-operator')
elif args[:2] == ['run', 'list']:
    print(42 if (root/'dispatched').exists() else 0)
elif args[:2] == ['workflow', 'run']:
    (root/'dispatched').touch()
elif args[:2] == ['release', 'upload']:
    with tarfile.open(args[3]) as archive:
        (root/'uploaded').write_text(json.dumps(archive.getnames()))
elif args[:2] == ['run', 'watch']:
    sys.exit(int(os.environ.get('PUBLISH_TEST_FAIL', '0')))
elif args[:2] == ['run', 'view']:
    print(json.dumps(dict(headSha='abc123',headBranch='main',conclusion='success',url='test-run')))
""")
        fake.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(binary) + os.pathsep + os.environ["PATH"],
            GITHUB_TOKEN="fake-test-token",
            GH_TOKEN="fake-test-token",
            PAUK_PUBLIC_DIR=str(public),
            PAUK_REPORTS_DIR=str(self.root / "reports"),
            PAUK_PAGES_RESULT=str(self.root / "result"),
            PUBLISH_TEST_ROOT=str(self.root),
        )
        self.script = Path(__file__).parents[2] / "scripts/publish_pages.sh"

    def run_script(self):
        return subprocess.run(
            ["bash", str(self.script)], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15
        )

    def test_only_four_public_files_uploaded_and_workflow_completion_required(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        uploaded = json.loads((self.root / "uploaded").read_text())
        self.assertNotIn("./private.json", uploaded)
        self.assertEqual(
            {Path(name).name for name in uploaded if name != "."},
            {"graph-data.json", "authors-detail.json", "repos-detail.json", "pubs-detail.json"},
        )
        self.assertEqual(json.loads((self.root / "result").read_text())["conclusion"], "success")
        self.assertIn('"watch"', (self.root / "calls").read_text())
        self.assertNotIn("fake-test-token", result.stdout + result.stderr)

    def test_personal_fields_refused_before_github_is_called(self):
        (self.root / "public/authors-detail.json").write_text('[{"key":"a0","email":"private@example.org"}]')
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((self.root / "calls").exists())

    def test_real_author_identifiers_are_refused(self):
        (self.root / "public/graph-data.json").write_text('{"authors":[{"key":"A12345"}]}')
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((self.root / "calls").exists())

    def test_failed_deployment_has_no_success_result(self):
        self.env["PUBLISH_TEST_FAIL"] = "1"
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((self.root / "result").exists())
