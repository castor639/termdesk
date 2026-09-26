"""sandbox cp paths, image updates and the agent guide, with docker mocked out."""
import json
import os
import tempfile
import unittest
from unittest import mock

from termdesk import cli, sandbox


class Copy(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def docker(args, timeout=None, check=True):
            self.calls.append(args)
            ok = True
            if args[0] == "inspect":
                ok = args[1] == "termdesk-work"
            elif args[:3] == ["exec", "termdesk-work", "test"]:
                ok = args[-1] == "/home/guest/docs"
            return mock.Mock(returncode=0 if ok else 1, stdout="", stderr="")

        for p in (mock.patch.object(sandbox, "_docker", side_effect=docker),
                  mock.patch.object(sandbox, "require_docker")):
            p.start()
            self.addCleanup(p.stop)
        f = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.file = f.name

    def test_into_a_folder_chowns_only_the_copy(self):
        final = sandbox.cp(self.file, "work:docs")
        self.assertEqual(final, "/home/guest/docs/" + os.path.basename(self.file))
        self.assertIn(["cp", self.file, "termdesk-work:/home/guest/docs"], self.calls)
        self.assertEqual(self.calls[-1], ["exec", "-u", "root", "termdesk-work", "chown", "-R", "guest:guest", "--", final])

    def test_to_a_new_name(self):
        self.assertEqual(sandbox.cp(self.file, "work:report.csv"), "/home/guest/report.csv")

    def test_out_of_the_sandbox(self):
        out = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, out)
        self.assertEqual(sandbox.cp("work:/tmp/a.png", out), os.path.join(out, "a.png"))
        self.assertIn(["cp", "termdesk-work:/tmp/a.png", out], self.calls)

    def test_bad_sides(self):
        with self.assertRaisesRegex(sandbox.SandboxError, "no sandbox named nope"):
            sandbox.cp(self.file, "nope:x")
        with self.assertRaisesRegex(sandbox.SandboxError, "one side"):
            sandbox.cp(self.file, self.file + ".copy")


class Pull(unittest.TestCase):
    def pull(self, image, label, pull=False, pulled_id="sha256:new", pull_ok=True):
        ids = {"before": "sha256:old" if label is not None else None}
        calls = []

        def docker(args, timeout=None, check=True):
            calls.append(args)
            if args[:2] == ["image", "inspect"]:
                iid = ids["after"] if "after" in ids else ids["before"]
                if iid is None:
                    return mock.Mock(returncode=1, stdout="")
                labels = {"termdesk.image": label} if label else None
                return mock.Mock(returncode=0, stdout=json.dumps([{"Id": iid, "Config": {"Labels": labels}}]))
            return mock.Mock(returncode=0, stdout="")

        def run(argv, stdout=None):
            ids["after"] = pulled_id
            return mock.Mock(returncode=0 if pull_ok else 1)

        with mock.patch.object(sandbox, "_docker", side_effect=docker), \
                mock.patch.object(sandbox.subprocess, "run", side_effect=run) as pulled, \
                mock.patch.object(sandbox.sys, "stderr"):
            sandbox._pull(image, pull)
        return pulled.called, [c for c in calls if c[0] == "rmi"]

    def test_current_default_image_is_used_as_is(self):
        self.assertEqual(self.pull(sandbox.DEFAULT_IMAGE, str(sandbox.IMAGE_VERSION)), (False, []))

    def test_old_default_image_is_replaced(self):
        self.assertEqual(self.pull(sandbox.DEFAULT_IMAGE, ""), (True, [["rmi", "sha256:old"]]))

    def test_old_image_stays_when_the_pull_fails(self):
        self.assertEqual(self.pull(sandbox.DEFAULT_IMAGE, "1", pull_ok=False), (True, []))

    def test_other_images_are_only_pulled_when_missing_or_asked(self):
        self.assertEqual(self.pull("termdesk-snap:clean", ""), (False, []))
        self.assertEqual(self.pull("myimage:dev", None)[0], True)
        with self.assertRaises(sandbox.SandboxError):
            self.pull("myimage:dev", None, pull_ok=False)


class Guide(unittest.TestCase):
    def test_guide_drops_the_front_matter(self):
        text = cli.guide_text()
        self.assertFalse(text.startswith("---"))
        self.assertNotIn("\ndescription:", text[:400])
        self.assertIn("termdesk action", text)

    def test_open_sends_absolute_host_paths(self):
        with tempfile.NamedTemporaryFile(suffix=".ods") as f:
            rel = os.path.relpath(f.name)
            self.assertEqual(cli.parse_action(["open", rel]), {"cmd": "open", "target": f.name})
        self.assertEqual(cli.parse_action(["open", "https://example.com"]),
                         {"cmd": "open", "target": "https://example.com"})


if __name__ == "__main__":
    unittest.main()
