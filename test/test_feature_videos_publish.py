"""The feature-videos publishing tool must produce a folder the runtime trusts.

Three properties carry this suite:

* **The manifest describes the bytes.** Every hash and size in ``manifest.json``
  is recomputed from the files on disk, so a manifest that agrees with itself but
  not with its media is a failure, not a pass.
* **Tampering is detected.** Each way a published folder can be altered — a
  media byte, a manifest field, a signature from another key, an unsigned extra
  file — is exercised and must be rejected.
* **The tool's copy of the rules matches the runtime's.** The tool owns its own
  canonical-JSON encoder and its own copy of nothing else: the doc allowlist is
  parsed from the runtime's source, and a signature the tool produces is fed to
  the runtime's own verifier. Those two cross-checks are what make a local copy
  safe, so a drift fails here rather than on a CDN.

The tool works in place: the operator assembles a release folder holding an
``<id>.mp4`` and an ``<id>.jpg`` per catalog entry, and the tool hashes them
where they sit and writes ``manifest.json`` beside them. So
the fixtures here are a folder plus a ``catalog.json`` kept outside it, and the
signed folder is the same folder the fixture built.

The clip fixture is the recorded placeholder already in the tree rather than a
synthesized container: a hand-built MP4 would hash and size fine while being
something no browser plays, which is the one thing a publishing tool must not
ship.
"""

from __future__ import annotations

import ast
import base64
import errno
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import feature_video_fixture as fixture
import pytest

from kiro_crew import feature_videos_manifest as fvm
from kiro_crew import platform_compat
from kiro_crew.platform import feed_trust
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "scripts" / "feature-videos"
PLACEHOLDER_CLIP = ROOT / "website" / "capture" / "assets" / "placeholder.mp4"
PLACEHOLDER_POSTER = ROOT / "website" / "capture" / "assets" / "placeholder.jpg"

#: A doc that is really in the tips allowlist, so the allowlist gate passes for
#: the happy path and a made-up name can test the refusal.
ALLOWED_DOC = "monitor-loops.md"


def _load(name: str) -> Any:
    """Import one of the tool's modules by path.

    The tool lives under ``scripts/`` and is not an importable package, which is
    deliberate — it must run from a checkout with no install.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest_mod = _load("_manifest")
publish_mod = _load("publish")
verify_mod = _load("verify")
ManifestError = manifest_mod.ManifestError


#: The consumer module whose limits this tool must stay under. Its numbers are
#: read as SOURCE rather than imported so the comparison sees the literals the
#: runtime declares, with nothing computed in between.
CONSUMER_SOURCE = ROOT / "src" / "kiro_crew" / "feature_videos_manifest.py"
CACHE_SOURCE = ROOT / "src" / "kiro_crew" / "feature_videos_cache.py"


def _number_literal(node: ast.expr) -> int | float | None:
    """Evaluate an int constant or a product/sum of them, e.g. ``64 * 1024``.

    Deliberately narrow: the consumer writes its limits as plain arithmetic, and
    anything else should read as "cannot determine" rather than be executed.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        left = _number_literal(node.left)
        right = _number_literal(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        return left + right if isinstance(node.op, ast.Add) else left - right
    return None


def _module_assignments(source: Path) -> dict[str, ast.expr]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node.value
    return out


def _consumer_limits() -> dict[str, int | float]:
    """The runtime's module-level numeric limits, read from its source.

    Two modules: the manifest parser bounds the document, each clip and a
    duration, the cache bounds a poster transfer.
    """
    limits: dict[str, int | float] = {}
    for source in (CONSUMER_SOURCE, CACHE_SOURCE):
        for name, value in _module_assignments(source).items():
            literal = _number_literal(value)
            if literal is not None:
                limits[name] = literal
    return limits


def _consumer_pattern(name: str) -> str:
    """The pattern string of a ``re.compile(r"...")`` assignment in the consumer."""
    value = _module_assignments(CONSUMER_SOURCE)[name]
    assert isinstance(value, ast.Call) and value.args, f"{name} is not a re.compile(...) call"
    pattern = value.args[0]
    assert isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
    return pattern.value


@pytest.fixture(scope="module")
def key_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway RSA-3072 pair. Module-scoped: keygen is the slow part.

    3072 bits rather than 2048 because the tool refuses a weaker release key,
    the same floor the CLI manifest signer enforces. A development key, never
    the production one — the production private half lives in KMS and cannot be
    read by anyone.
    """
    return fixture.mint_throwaway_key(tmp_path_factory.mktemp("feature-videos-key"))


@pytest.fixture(scope="module")
def second_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A second RSA-3072 private key, for forging a signature by the wrong key."""
    private, _public = fixture.mint_throwaway_key(
        tmp_path_factory.mktemp("feature-videos-second-key")
    )
    return private


def _entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "id": "monitor-loops",
        "feature": "monitor-loops",
        "title": "Let one session watch a pull request",
        "description": "A monitor loop re-injects your check instructions on an interval.",
        "doc": ALLOWED_DOC,
        "used_when": ["sel_event_seen:monitor_start"],
        "min_version": "",
        "duration_s": 22.0,
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def release_dir(tmp_path: Path) -> Path:
    """A release folder holding one real clip and its poster, ready to be signed."""
    folder = tmp_path / "release"
    folder.mkdir()
    shutil.copyfile(PLACEHOLDER_CLIP, folder / "monitor-loops.mp4")
    shutil.copyfile(PLACEHOLDER_POSTER, folder / "monitor-loops.jpg")
    return folder


@pytest.fixture()
def catalog(tmp_path: Path) -> Path:
    """The catalog describing that folder, kept outside it.

    Outside because the tool refuses a release folder carrying any file the
    catalog does not name, and the catalog is not one of them.
    """
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"entries": [_entry()]}, indent=2) + "\n", encoding="utf-8")
    return path


def _rewrite_catalog(catalog_path: Path, entry: dict[str, Any]) -> None:
    catalog_path.write_text(json.dumps({"entries": [entry]}, indent=2) + "\n", encoding="utf-8")


_posix_publisher = pytest.mark.skipif(
    os.name == "nt",
    reason="publish.py refuses to sign on Windows (owner-only rule is unjudgeable there)",
)


def _publish(
    release_dir: Path, catalog: Path, private_key: Path, *extra: str, release: str = "0.7.0"
) -> Path:
    if os.name == "nt":
        pytest.skip("publish.py refuses to sign on Windows (owner-only rule is unjudgeable there)")
    exit_code = publish_mod.main(
        [
            "--catalog",
            str(catalog),
            "--release-dir",
            str(release_dir),
            "--cdn-host",
            "videos.example.com",
            "--release",
            release,
            "--signing-key",
            str(private_key),
            *extra,
        ]
    )
    assert exit_code == 0
    return release_dir


def _read(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))


def _rewrite(folder: Path, manifest: dict[str, Any]) -> None:
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


class _FakeKms:
    """Stands in for the AWS CLI at the signer's `_run_aws_json` seam.

    Answers `kms get-public-key` with *public*'s DER and `kms sign` with a real
    PKCS#1 v1.5 signature over the digest it is handed, made with *private* --
    the same bytes KMS would return for that key.
    """

    def __init__(self, private: Path, public: Path, scratch: Path) -> None:
        self.private = private
        self.public = public
        self.scratch = scratch
        self.calls: list[str] = []

    def __call__(self, args: list[str]) -> dict[str, Any]:
        assert args[0] == "kms"
        self.calls.append(args[1])
        openssl = fixture.openssl_or_skip()
        if args[1] == "get-public-key":
            der = subprocess.run(
                [openssl, "pkey", "-pubin", "-in", str(self.public), "-outform", "DER"],
                check=True,
                cwd=self.scratch,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout
            return {
                "KeyUsage": "SIGN_VERIFY",
                "KeySpec": "RSA_3072",
                "SigningAlgorithms": ["RSASSA_PKCS1_V1_5_SHA_256"],
                "PublicKey": base64.b64encode(der).decode("ascii"),
            }
        assert args[1] == "sign"
        digest = base64.b64decode(args[args.index("--message") + 1], validate=True)
        digest_path = self.scratch / "digest.bin"
        digest_path.write_bytes(digest)
        signature = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-sign",
                "-inkey",
                str(self.private),
                "-in",
                str(digest_path),
                "-pkeyopt",
                "digest:sha256",
            ],
            check=True,
            cwd=self.scratch,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        return {"Signature": base64.b64encode(signature).decode("ascii")}


def _sign_bytes(payload: bytes, private_key: Path, tmp_path: Path) -> str:
    path = tmp_path / "payload-to-sign.json"
    path.write_bytes(payload)
    signature = subprocess.run(
        [fixture.openssl_or_skip(), "dgst", "-sha256", "-sign", str(private_key), str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return base64.b64encode(signature).decode("ascii")


class TestProducedFolder:
    def test_manifest_has_the_contract_shape(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        private, _ = key_pair
        manifest = _read(_publish(release_dir, catalog, private))

        # key_id is absent for a local key: it is a hint about which PINNED key
        # signed, and a staging key is not one.
        assert set(manifest) == {
            "schema",
            "release",
            "cdn_base",
            "generated_at",
            "entries",
            "signature",
        }
        assert manifest["schema"] == "kirocrew-feature-videos-manifest-v1"
        assert manifest["release"] == "0.7.0"
        assert manifest["cdn_base"] == "https://videos.example.com/feature-videos/"
        assert manifest["generated_at"].endswith("Z")
        assert isinstance(manifest["signature"], str) and manifest["signature"]
        assert set(manifest["entries"][0]) == {
            "id",
            "feature",
            "title",
            "description",
            "file",
            "poster",
            "sha256",
            "poster_sha256",
            "bytes",
            "duration_s",
            "doc",
            "used_when",
            "min_version",
        }
        entry = manifest["entries"][0]
        assert entry["file"] == "monitor-loops.mp4"
        assert entry["poster"] == "monitor-loops.jpg"
        assert entry["used_when"] == ["sel_event_seen:monitor_start"]
        assert entry["min_version"] == ""
        assert entry["duration_s"] == 22.0

    def test_folder_carries_the_media_and_the_manifest_only(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """The signed folder is exactly the media plus manifest.json: nothing unsigned."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        names = {path.name for path in folder.iterdir()}
        assert names == {"monitor-loops.mp4", "monitor-loops.jpg", "manifest.json"}
        assert verify_mod.verify_folder(folder, public_key=public)["entries"] == 1

    def test_hashes_and_size_describe_the_real_bytes(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        folder = _publish(release_dir, catalog, key_pair[0])
        entry = _read(folder)["entries"][0]
        clip = folder / "monitor-loops.mp4"
        poster = folder / "monitor-loops.jpg"
        assert entry["sha256"] == hashlib.sha256(clip.read_bytes()).hexdigest()
        assert entry["poster_sha256"] == hashlib.sha256(poster.read_bytes()).hexdigest()
        assert entry["bytes"] == clip.stat().st_size

    def test_the_upload_is_printed_and_never_run(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Credentials stay with the human: the tool may only print the commands."""

        def _refuse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("publish.py must never invoke the AWS CLI")

        # The AWS runner is the shared signer's; a local-key publish must never reach it.
        monkeypatch.setattr(manifest_mod._signer, "_run_aws_json", _refuse)
        _publish(release_dir, catalog, key_pair[0])
        out = capsys.readouterr().out
        assert "aws s3 sync --dryrun" in out
        assert "s3://<BUCKET>/feature-videos/0.7.0/" in out
        assert "aws cloudfront create-invalidation --distribution-id <DISTRIBUTION_ID>" in out
        assert "'/feature-videos/0.7.0/*'" in out

    def test_the_upload_plan_quotes_the_folder_for_the_shell(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A folder path with a space is pasted into a shell; it must stay one word.

        Unquoted, `aws s3 sync dist/my release/ ...` uploads `dist/my` and treats
        `release/` as the destination. Every rendered path is `shlex.quote`d.
        """
        folder = tmp_path / "my release" / "0.7.0"
        publish_mod._print_upload_plan(folder, "0.7.0")
        out = capsys.readouterr().out
        quoted = shlex.quote(str(folder))
        assert quoted != str(folder)
        assert f"aws s3 sync --dryrun {quoted}/ s3://<BUCKET>/feature-videos/0.7.0/" in out
        assert f"aws s3 sync {quoted}/ s3://<BUCKET>/feature-videos/0.7.0/" in out
        assert f"verify.py {quoted}" in out
        assert f"sync {folder}/" not in out

    def test_the_runtime_derives_the_url_the_upload_plan_publishes_to(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path], capsys: Any
    ) -> None:
        """The URL a dashboard fetches must be the object the operator uploaded.

        The runtime builds ``<cdn_base>/<release>/<name>`` itself, and the upload
        plan puts the folder under ``feature-videos/<release>/``. The manifest's
        ``cdn_base`` is the only thing joining the two, so it is checked end to
        end here: a base already carrying the release would double it and every
        clip would 404 on every dashboard.
        """
        folder = _publish(release_dir, catalog, key_pair[0])
        prefix = "feature-videos/0.7.0/"
        assert f"s3://<BUCKET>/{prefix}" in capsys.readouterr().out
        parsed = fvm.parse_manifest(_read(folder))
        assert parsed is not None and parsed.entries
        entry = parsed.entries[0]
        assert parsed.asset_url(entry.file) == f"https://videos.example.com/{prefix}{entry.file}"
        assert parsed.asset_url(entry.poster) == (
            f"https://videos.example.com/{prefix}{entry.poster}"
        )

    def test_a_local_key_says_the_artifact_is_not_a_release(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Separate keys for staging and production, and the output must say which."""
        _publish(release_dir, catalog, key_pair[0])
        assert "staging artifact" in capsys.readouterr().err

    def test_the_signing_key_is_never_printed(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No signing-tool output may carry key material or its bytes."""
        private, _ = key_pair
        _publish(release_dir, catalog, private)
        captured = capsys.readouterr()
        secret = private.read_text(encoding="utf-8")
        body = "".join(secret.splitlines()[1:-1])[:64]
        for stream in (captured.out, captured.err):
            assert "PRIVATE KEY" not in stream
            assert body not in stream


class TestRulesMatchTheRuntime:
    """The tool's local copies must not drift from what the runtime does."""

    def test_the_runtime_limits_are_the_consumer_s(self) -> None:
        """Every ceiling this tool enforces is the runtime's own number.

        The runtime's numbers are read from its source rather than restated here,
        so a runtime that tightens fails this test instead of letting the tool
        sign a release the dashboard refuses. Exact equality: the tool has no
        ceilings of its own, and no flag to loosen one.
        """
        limits = _consumer_limits()
        pairs = (
            ("max_payload_bytes", "_SIGNED_PAYLOAD_MAX_BYTES"),
            ("max_document_bytes", "_MANIFEST_MAX_BYTES"),
            ("max_entries", "_MAX_ENTRIES"),
            ("max_clip_bytes", "_MAX_ENTRY_BYTES"),
            ("max_poster_bytes", "MAX_POSTER_BYTES"),
            ("max_duration_s", "_MAX_DURATION_S"),
        )
        assert set(manifest_mod.RUNTIME_LIMITS) == {flag for flag, _ in pairs}
        for flag, constant in pairs:
            runtime = limits.get(constant)
            assert runtime is not None, f"{constant} is missing from the consumer"
            assert manifest_mod.RUNTIME_LIMITS[flag] == runtime, (
                f"RUNTIME_LIMITS[{flag!r}] restates {constant} as "
                f"{manifest_mod.RUNTIME_LIMITS[flag]}, the consumer says {runtime}"
            )

    def test_the_signing_plumbing_is_the_cli_signer_s(self) -> None:
        """One signer for the CLI feed and for videos: the tool shares its code.

        `packaging/signing/cli-manifest.py` is loaded by path and its canonical
        form, key-id derivation, runners, KMS flow and error class are the very
        objects this tool uses -- identity, not equality, so there is no second
        copy that could drift.
        """
        signer = manifest_mod._signer
        assert signer.__file__ == str(ROOT / "packaging" / "signing" / "cli-manifest.py")
        assert manifest_mod.ManifestError is signer.ManifestError
        assert manifest_mod.public_key_id is signer.public_key_id
        assert manifest_mod.public_key_der is signer.public_key_der is signer._public_key_der
        assert manifest_mod.run_openssl is signer.run_openssl is signer._run_openssl
        assert manifest_mod.MAX_SIGNATURE_BYTES == signer.MAX_SIGNATURE_BYTES
        assert manifest_mod.kms_sign_digest is signer.kms_sign_digest
        assert manifest_mod.ALGORITHM == signer.ALGORITHM == "RSASSA_PKCS1_V1_5_SHA_256"

    def test_a_windows_path_hit_must_sit_under_an_administrator_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows the owner check is "under a root only administrators write".

        Exercised on every platform by pretending to be Windows: the roots the OS
        would report are replaced by one temp directory, a tool under it is
        accepted, and the same tool one directory over is refused. Case and
        trailing separators must not matter, and a root that is a prefix of a
        sibling's name must not match it.
        """
        signer = manifest_mod._signer
        admin_root = tmp_path / "Program Files"
        (admin_root / "Git" / "usr" / "bin").mkdir(parents=True)
        inside = admin_root / "Git" / "usr" / "bin" / "openssl.exe"
        inside.write_bytes(b"")
        outside = tmp_path / "Program Files Lookalike" / "openssl.exe"
        outside.parent.mkdir()
        outside.write_bytes(b"")
        monkeypatch.setattr(signer.os, "name", "nt")
        monkeypatch.setattr(signer, "_windows_admin_roots", lambda: (str(admin_root) + os.sep,))
        assert signer._only_owner_writes(str(inside)) is True
        assert signer._only_owner_writes(str(outside)) is False
        assert signer._only_owner_writes(str(tmp_path / "openssl.exe")) is False
        # The directory PATH found the name in must lie under an admin root too: a
        # link in the profile pointing at the real Program Files binary is refused.
        assert signer._only_owner_writes(str(inside), str(outside.parent)) is False
        assert signer._only_owner_writes(str(inside), str(inside.parent)) is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory permission bits")
    def test_the_runners_never_invoke_a_bare_tool_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`openssl` and `aws` are resolved to absolute paths, never looked up by PATH.

        A writable PATH entry ahead of the system one would otherwise hand a
        planted binary the private-key path or the KMS authority. A system-directory
        hit wins outright. A PATH-only hit is refused when its directory can be
        written by others (an entry can be swapped), when the file itself can (the
        code can be rewritten in place), or when it is a link into such a file; a
        Homebrew-shaped prefix the operator owns with default permissions passes.
        """
        signer = manifest_mod._signer
        resolved = signer._trusted_tool("openssl")
        assert os.path.isabs(resolved)
        assert os.path.dirname(resolved) in signer._TRUSTED_BIN_DIRS

        planted = tmp_path / "planted-bin"
        planted.mkdir()
        planted.chmod(0o777)
        shim = planted / "not-a-real-tool"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(planted))
        with pytest.raises(ManifestError, match="written by another user"):
            signer._trusted_tool("not-a-real-tool")
        planted.chmod(0o755)
        assert signer._trusted_tool("not-a-real-tool") == str(shim.resolve())
        # The directory is owner-only but the file is group-writable: a group member
        # rewrites the code in place without touching the directory. Refused, and the
        # refusal names the file with its mode.
        shim.chmod(0o775)
        with pytest.raises(ManifestError, match=r"not-a-real-tool \(mode 0775"):
            signer._trusted_tool("not-a-real-tool")
        shim.chmod(0o755)
        # A link in an owner-only directory into a world-writable file elsewhere is
        # the same defect one hop away; the check runs on the real file.
        loose = tmp_path / "loose"
        loose.mkdir()
        target = loose / "real-tool"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o777)
        (planted / "linked-tool").symlink_to(target)
        with pytest.raises(ManifestError, match="written by another user"):
            signer._trusted_tool("linked-tool")
        with pytest.raises(ManifestError, match="required and was not found"):
            signer._trusted_tool("no-such-tool-anywhere")
        # The other direction: the real file and its directory are owner-only, but
        # the PATH directory the name was found in is not. Whoever can write that
        # directory re-points the link at any file that passes the first two checks
        # -- a root-owned `cat`, say, which would print the key it is handed. The
        # entry the search followed is a deciding one, so it is checked too.
        sound = tmp_path / "sound"
        sound.mkdir()
        sound.chmod(0o755)
        genuine = sound / "genuine-tool"
        genuine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        genuine.chmod(0o755)
        planted.chmod(0o777)
        (planted / "genuine-tool").symlink_to(genuine)
        with pytest.raises(ManifestError, match=f"through {re.escape(str(planted.resolve()))}"):
            signer._trusted_tool("genuine-tool")
        planted.chmod(0o755)
        assert signer._trusted_tool("genuine-tool") == str(genuine.resolve())
        # A group write bit on the lookup directory alone is tolerated, as the
        # runtime's provider-binary rule tolerates it: this is Intel Homebrew's and
        # the GitHub macOS image's `/usr/local/bin` (`user:admin 0775`). The real
        # file and its own directory still refuse a group write bit (above).
        planted.chmod(0o775)
        assert signer._trusted_tool("genuine-tool") == str(genuine.resolve())
        planted.chmod(0o755)

        # Homebrew's default layout with its default permissions, owned by the
        # operator: `<prefix>/bin/<tool>` is a link into `<prefix>/Cellar/...`, every
        # directory 0755 and the binary 0755. This is what the release maintainer's
        # macOS host looks like, and it must keep working.
        prefix = tmp_path / "homebrew"
        cellar_bin = prefix / "Cellar" / "openssl@3" / "3.3.1" / "bin"
        cellar_bin.mkdir(parents=True)
        for directory in (prefix, prefix / "Cellar", cellar_bin.parent.parent, cellar_bin.parent):
            directory.chmod(0o755)
        cellar_bin.chmod(0o755)
        brew_tool = cellar_bin / "brew-tool"
        brew_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        brew_tool.chmod(0o755)
        (prefix / "bin").mkdir()
        (prefix / "bin").chmod(0o755)
        (prefix / "bin" / "brew-tool").symlink_to(brew_tool)
        monkeypatch.setenv("PATH", str(prefix / "bin"))
        assert signer._trusted_tool("brew-tool") == str(brew_tool.resolve())
        monkeypatch.setenv("PATH", str(planted))

        # The by-name list is the runtime's own: root-owned system directories, and
        # never a user-owned install prefix such as Homebrew's or the local prefix.
        assert set(signer._TRUSTED_BIN_DIRS) == set(platform_compat._TRUSTED_SYSTEM_BIN_DIRS)
        assert "/usr/local/bin" not in signer._TRUSTED_BIN_DIRS
        assert "/opt/homebrew/bin" not in signer._TRUSTED_BIN_DIRS

        # And the runners go through it: the argv they hand subprocess starts with
        # the resolved absolute path, never the bare name.
        seen: list[list[str]] = []

        def record(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")

        monkeypatch.setattr(signer.subprocess, "run", record)
        monkeypatch.setattr(signer, "_TRUSTED_BIN_DIRS", (os.path.dirname(resolved),))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        signer.run_openssl(["version"])
        assert seen[-1][0] == resolved
        if shutil.which("aws"):
            signer._run_aws_json(["sts", "get-caller-identity"])
            assert os.path.isabs(seen[-1][0]) and seen[-1][0] != "aws"

    def test_the_release_image_s_own_tools_pass_the_trust_check(self) -> None:
        """`_trusted_tool` accepts the real `openssl` and `aws` of the machine CI runs on.

        CI runs this on `ubuntu-latest`, the image `publish-cli.yml` signs releases
        on, and on the `windows-latest` and `macos` runners, so an image update
        that moves either tool into a place the check refuses fails here, before a
        release does. A tool that is not installed is not an answer either way, so
        it is skipped rather than passed.
        """
        signer = manifest_mod._signer
        present = [name for name in ("openssl", "aws") if shutil.which(name)]
        if not present:
            pytest.skip("neither openssl nor aws is installed here")
        for name in present:
            resolved = signer._trusted_tool(name)
            assert os.path.isabs(resolved), (name, resolved)
            assert os.path.isfile(resolved) and os.access(resolved, os.X_OK), (name, resolved)

    def test_canonical_bytes_is_the_signer_s_rule_not_a_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper adds error mapping only; the bytes come from the signer."""
        monkeypatch.setattr(manifest_mod._signer, "canonical_json", lambda value: b"sentinel\n")
        assert manifest_mod.canonical_bytes({"a": 1}) == b"sentinel\n"

    @_posix_publisher
    def test_the_kms_path_signs_through_the_shared_pinned_flow(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--kms-key-arn` publishes a release the pinned key verifies.

        The AWS CLI is replaced at the signer's runner: `get-public-key` answers
        with the throwaway key's DER and `sign` signs the digest it is handed with
        the throwaway private key, exactly as KMS would. The committed key is the
        throwaway public half for the duration, so the pin check is exercised for
        real and `key_id` in the manifest is its identity.
        """
        private, public = key_pair
        monkeypatch.setattr(publish_mod, "PUBLIC_KEY_PATH", public)
        fake = _FakeKms(private, public, tmp_path)
        monkeypatch.setattr(manifest_mod._signer, "_run_aws_json", fake)

        exit_code = publish_mod.main(
            [
                "--catalog",
                str(catalog),
                "--release-dir",
                str(release_dir),
                "--cdn-host",
                "videos.example.com",
                "--release",
                "0.7.0",
                "--kms-key-arn",
                "arn:aws:kms:us-west-2:000000000000:key/fixture",
            ]
        )
        assert exit_code == 0
        assert fake.calls == ["get-public-key", "sign"]
        manifest = _read(release_dir)
        assert manifest["key_id"] == manifest_mod.public_key_id(public)
        assert verify_mod.verify_folder(release_dir, public_key=public)["claims_key_id"] is True

    @_posix_publisher
    def test_a_kms_key_that_is_not_the_committed_one_is_refused_before_signing(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        second_key: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mistyped ARN must not produce a folder every dashboard refuses.

        KMS answers with some other key's public half; the pin check refuses it,
        `sign` is never called, and nothing is written.
        """
        _private, public = key_pair
        monkeypatch.setattr(publish_mod, "PUBLIC_KEY_PATH", public)
        other_public = tmp_path / "other-public.pem"
        subprocess.run(
            [
                fixture.openssl_or_skip(),
                "pkey",
                "-in",
                str(second_key),
                "-pubout",
                "-out",
                str(other_public),
            ],
            check=True,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake = _FakeKms(second_key, other_public, tmp_path)
        monkeypatch.setattr(manifest_mod._signer, "_run_aws_json", fake)

        with pytest.raises(ManifestError, match="does not match the committed public key"):
            publish_mod.main(
                [
                    "--catalog",
                    str(catalog),
                    "--release-dir",
                    str(release_dir),
                    "--cdn-host",
                    "videos.example.com",
                    "--release",
                    "0.7.0",
                    "--kms-key-arn",
                    "arn:aws:kms:us-west-2:000000000000:key/wrong",
                ]
            )
        assert fake.calls == ["get-public-key"]
        assert not (release_dir / "manifest.json").exists()

    def test_the_longest_id_still_fits_the_runtime_basename_bound(self) -> None:
        """``<id>.mp4`` is checked by the runtime's basename regex, id included.

        The id cap is derived from that bound, so it is checked against the
        runtime's own pattern: the longest id this tool signs must pass, and one
        character more must not, or the derivation has drifted.
        """
        basename_re = re.compile(_consumer_pattern("_SAFE_BASENAME_RE"))
        longest = "a" * manifest_mod.MAX_ID_CHARS
        assert manifest_mod.validate_slug(longest, where="test") == longest
        for suffix in (".mp4", ".jpg"):
            assert basename_re.match(longest + suffix), f"{len(longest)}-char id + {suffix}"
        assert not basename_re.match("a" * (manifest_mod.MAX_ID_CHARS + 1) + ".mp4")
        with pytest.raises(ManifestError, match="longer than"):
            manifest_mod.validate_slug("a" * (manifest_mod.MAX_ID_CHARS + 1), where="test")

    @pytest.mark.parametrize(
        ("release", "signable"),
        [
            ("0.7.0", True),
            ("0.0.0", True),
            ("10.20.30", True),
            ("99999.99999.99999", True),
            ("00.7.0", False),
            ("0.07.0", False),
            ("0.7.00", False),
            ("1", False),
            ("1.2", False),
            ("1.2.3.4", False),
            ("123456.0.0", False),
            ("0..1", False),
            ("0.7.0-rc1", False),
        ],
    )
    def test_a_release_is_a_folder_the_runtime_will_ask_for(
        self, release: str, signable: bool
    ) -> None:
        """Only ``major.minor.patch`` is signed: the one shape a dashboard fetches.

        The runtime's parser admits one to four components, but its fetch side
        formats every candidate folder as three, so a `1.2/` release would be
        signed, verified, uploaded and never requested. Every shape signed here
        must both parse on the runtime and be one it asks for.
        """
        runtime_re = re.compile(_consumer_pattern("_RELEASE_RE"))
        if signable:
            assert manifest_mod.validate_release(release) == release
            assert runtime_re.match(release), f"runtime would refuse {release!r}"
        else:
            with pytest.raises(ManifestError, match="three-component"):
                manifest_mod.validate_release(release)

    def test_every_folder_the_runtime_asks_for_is_a_signable_release(self) -> None:
        """The runtime's own candidate list, fed back through the publisher's rule."""
        candidates = fvm.release_candidates("0.7.3")
        assert candidates, "the runtime returned no candidate folders"
        for folder in candidates:
            assert manifest_mod.validate_release(folder) == folder

    def test_a_signable_release_is_the_folder_name_the_runtime_derives_from_it(self) -> None:
        """The rule is the runtime's round trip, not a grammar of its own.

        `running_release` is the runtime's folder name for a version. Whatever the
        publisher signs must come back from it unchanged -- otherwise the folder is
        uploaded under one name and requested under another, and never fetched --
        and whatever does not come back unchanged must be refused. Leading zeros,
        padding and truncation are the known ways a name fails the trip.
        """
        corpus = [
            "0.7.0",
            "0.0.0",
            "1.0.0",
            "10.20.30",
            "99999.99999.99999",
            "00.7.0",
            "0.07.0",
            "0.7.00",
            "007.7.7",
            "0.7.0rc1",
            "0.7.0-rc1",
            "0.7.0+build.1",
            "1",
            "1.2",
            "1.2.3.4",
            "0..1",
            " 0.7.0",
            "0.7.0 ",
        ]
        for release in corpus:
            try:
                accepted = manifest_mod.validate_release(release) == release
            except ManifestError:
                accepted = False
            round_trips = fvm.running_release(release) == release
            if accepted:
                assert round_trips, f"{release!r} is signed but the runtime asks for another folder"
            if not round_trips:
                assert not accepted, f"{release!r} does not round-trip and must be refused"

    @_posix_publisher
    def test_a_duration_over_the_runtime_ceiling_is_refused(self, catalog: Path) -> None:
        """The runtime reads a duration over its ceiling as unknown, so it is refused here.

        The ceiling is the runtime's own (`_MAX_DURATION_S`, pinned through
        `RUNTIME_LIMITS`); a value exactly at it is kept, one millisecond over is not.
        """
        ceiling = manifest_mod.RUNTIME_LIMITS["max_duration_s"]
        _rewrite_catalog(catalog, _entry(duration_s=ceiling))
        assert publish_mod.load_catalog(catalog)[0]["duration_s"] == ceiling
        _rewrite_catalog(catalog, _entry(duration_s=ceiling + 0.001))
        with pytest.raises(ManifestError, match=f"over the runtime's {ceiling} second ceiling"):
            publish_mod.load_catalog(catalog)
        assert fvm._bounded_duration(ceiling + 0.001) == 0.0

    @_posix_publisher
    def test_a_duplicate_id_is_refused(self, catalog: Path) -> None:
        catalog.write_text(
            json.dumps({"entries": [_entry(), _entry()]}, indent=2) + "\n", encoding="utf-8"
        )
        with pytest.raises(ManifestError, match="duplicate id"):
            publish_mod.load_catalog(catalog)

    @_posix_publisher
    def test_an_unknown_catalog_field_is_refused(self, catalog: Path) -> None:
        _rewrite_catalog(catalog, _entry(src="/app-assets/feature-videos/x.mp4"))
        with pytest.raises(ManifestError, match="unknown field"):
            publish_mod.load_catalog(catalog)

    @_posix_publisher
    def test_a_non_finite_duration_is_refused(self, catalog: Path) -> None:
        """`> 0` admits infinity, and json.dumps writes the bare token Infinity.

        The signature over those bytes verifies perfectly while the document is
        not JSON, so the check has to be finiteness, not positivity.
        """
        _rewrite_catalog(catalog, _entry(duration_s=1e999))
        with pytest.raises(ManifestError, match="must be finite"):
            publish_mod.load_catalog(catalog)

    @_posix_publisher
    def test_an_out_of_range_duration_refuses_instead_of_crashing(self, catalog: Path) -> None:
        """A JSON integer has no width limit, and float() on a huge one raises.

        OverflowError is neither ValueError nor TypeError, so an uncaught one
        leaves a traceback where a refusal belongs.
        """
        catalog.write_text(
            '{"entries": [{"id": "monitor-loops", "feature": "monitor-loops", '
            '"title": "t", "description": "d", "doc": "monitor-loops.md", '
            '"used_when": [], "min_version": "", "duration_s": ' + "9" * 400 + "}]}\n",
            encoding="utf-8",
        )
        with pytest.raises(ManifestError, match="out of range"):
            publish_mod.load_catalog(catalog)

    @_posix_publisher
    def test_a_duration_that_rounds_to_zero_is_refused(self, catalog: Path) -> None:
        """The SIGNED value is what must be positive, not the value before rounding.

        0.0004 passes a bare `> 0` and then rounds to 0.0, which this tool's own
        verifier refuses — so publishing it produces a folder nobody can validate.
        """
        _rewrite_catalog(catalog, _entry(duration_s=0.0004))
        with pytest.raises(ManifestError, match="positive after rounding"):
            publish_mod.load_catalog(catalog)

    @_posix_publisher
    def test_a_duration_at_one_millisecond_is_kept(self, catalog: Path) -> None:
        """The boundary the rounding rule allows, so the refusal is not overbroad."""
        _rewrite_catalog(catalog, _entry(duration_s=0.001))
        assert publish_mod.load_catalog(catalog)[0]["duration_s"] == 0.001

    def test_an_oversize_catalog_is_refused_without_being_read_whole(self, catalog: Path) -> None:
        """The limit must gate the read, not follow it.

        Reading the file and measuring it afterwards makes the cap decorative: a
        multi-gigabyte input exhausts memory before the check that should have
        refused it. Asserted through the bounded reader directly, with a limit far
        below the file, so a regression to read-then-measure fails here.
        """
        with pytest.raises(ManifestError, match="larger than 8 bytes"):
            manifest_mod.read_bounded(catalog, limit=8)

    def test_the_bounded_reader_accepts_a_file_at_the_limit(self, tmp_path: Path) -> None:
        """Exactly at the cap is allowed; one byte over is not."""
        path = tmp_path / "payload.bin"
        path.write_bytes(b"0123456789")
        assert manifest_mod.read_bounded(path, limit=10) == b"0123456789"
        with pytest.raises(ManifestError, match="larger than 9 bytes"):
            manifest_mod.read_bounded(path, limit=9)

    @_posix_publisher
    def test_a_prerelease_min_version_is_refused(self, catalog: Path) -> None:
        _rewrite_catalog(catalog, _entry(min_version="0.8.0rc1"))
        with pytest.raises(ManifestError, match="bare release"):
            publish_mod.load_catalog(catalog)

    def test_a_non_https_cdn_base_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="must be an https URL"):
            manifest_mod.validate_cdn_base("http://videos.example.com/feature-videos/0.7.0/")

    def test_a_cdn_base_without_a_trailing_slash_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="must end with a slash"):
            manifest_mod.validate_cdn_base("https://videos.example.com/feature-videos/0.7.0")

    def test_a_malformed_cdn_base_is_refused_not_crashed(self) -> None:
        """urlsplit raises ValueError on an unbalanced bracket; the CLI must not."""
        with pytest.raises(ManifestError, match="not a well-formed URL"):
            manifest_mod.validate_cdn_base("https://[videos.example.com/feature-videos/")

    def test_a_malformed_cdn_port_is_refused_before_it_is_signed(self) -> None:
        """`urlsplit` parses the port lazily; the tool reads it so `:abc` fails here.

        A base with a bad port passes every other check and would be signed, and
        then every dashboard's media download fails on it. A well-formed port is
        kept.
        """
        for bad in (
            "https://videos.example.com:abc/feature-videos/",
            "https://videos.example.com:70000/feature-videos/",
            "https://videos.example.com:-1/feature-videos/",
            "https://videos.example.com:0/feature-videos/",
        ):
            with pytest.raises(ManifestError, match="not a well-formed URL"):
                manifest_mod.validate_cdn_base(bad)
        good = "https://videos.example.com:8443/feature-videos/"
        assert manifest_mod.validate_cdn_base(good) == good

    def test_a_cdn_host_carrying_a_path_is_refused(self) -> None:
        """`--cdn-host` is a host; a path in it would be signed into every asset URL.

        The upload plan names `s3://<BUCKET>/feature-videos/<release>/`, so a host
        of `videos.example.com/archive` would put `/archive` in every signed URL
        and nothing in the bucket -- every dashboard request would miss. A bare
        host, with or without a port, is kept as given.
        """
        for bad in (
            "videos.example.com/archive",
            "videos.example.com/",
            "videos.example.com?x=1",
            "videos.example.com#frag",
            "user@videos.example.com",
            "user:secret@videos.example.com",
            "",
            "videos.example.com:abc",
            "videos.example.com:0",
            "[videos.example.com",
            # `urlsplit` tolerates these inside the authority; a URL library does
            # not, so every download would fail with InvalidURL.
            "videos example.com",
            "videos_example.com",
            "-videos.example.com",
            "videos..example.com",
            "videos.example.com.",
            "vid\u00e9os.example.com",
            "a" * 64 + ".example.com",
            ".".join(["abc"] * 70),
        ):
            with pytest.raises(ManifestError, match="--cdn-host"):
                manifest_mod.validate_cdn_host(bad)
        for good in (
            "videos.example.com",
            "videos.example.com:8443",
            "cdn.example.com",
            "cdn-1.eu-west-1.example.com",
            "localhost",
            "192.0.2.10",
            "192.0.2.10:8443",
            "[2001:db8::10]",
            "[2001:db8::10]:8443",
        ):
            assert manifest_mod.validate_cdn_host(good) == good
        # And the CLI refuses before it reads the catalog: the file named here does
        # not exist, and a refusal about it would mean the host was accepted. (On
        # Windows the CLI refuses earlier still, before any argument is judged.)
        if os.name == "nt":
            return
        with pytest.raises(ManifestError, match="--cdn-host must be a host name"):
            publish_mod.main(
                [
                    "--catalog",
                    "does-not-exist.json",
                    "--cdn-host",
                    "videos.example.com/archive",
                    "--kms-key-arn",
                    "arn:aws:kms:us-west-2:000000000000:key/unused",
                ]
            )

    def test_a_deeply_nested_catalog_is_refused_not_crashed(self, catalog: Path) -> None:
        """json.loads raises RecursionError past the interpreter's depth; refuse it."""
        depth = 100_000
        catalog.write_text("[" * depth + "]" * depth, encoding="utf-8")
        with pytest.raises(ManifestError, match="nested too deeply"):
            manifest_mod.load_json_object(catalog, limit=1024 * 1024)

    def test_a_weak_signing_key_is_refused(self, tmp_path: Path) -> None:
        """Below 3072 bits is not a release key, the CLI signer's own floor."""
        openssl = fixture.openssl_or_skip()
        private = tmp_path / "weak.pem"
        subprocess.run(
            [
                openssl,
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(private),
            ],
            check=True,
            # cwd=tmp_path: a child must not inherit the checkout as its working
            # directory, so any relative file it writes lands in the temp dir.
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        public = tmp_path / "weak-public.pem"
        subprocess.run(
            [openssl, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            check=True,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with pytest.raises(ManifestError, match="at least 3072 bits"):
            manifest_mod.public_key_id(public)

    def test_the_doc_allowlist_is_the_runtime_s_own_object(self) -> None:
        """Imported from `kiro_crew.tips_allowlist`, so there is no second copy to drift."""
        assert publish_mod.TIP_DOC_ALLOWLIST is TIP_DOC_ALLOWLIST

    def test_the_runtime_verifier_accepts_what_the_tool_signs(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The canonical-JSON copy is pinned to the runtime's own verifier.

        The tool encodes the signed bytes itself, so the only thing that proves
        the copy agrees with the consumer is feeding a folder the tool signed to
        the consumer's verifier. A one-byte edit must flip the verdict.
        """
        private, public = key_pair
        manifest = _read(_publish(release_dir, catalog, private))
        fixture.pin_fixture_key(monkeypatch, public)

        cap = manifest_mod.RUNTIME_LIMITS["max_payload_bytes"]
        assert feed_trust.verify_document_signature(manifest, max_payload_bytes=cap) is True

        manifest["entries"][0]["title"] = "A title nobody signed"
        assert feed_trust.verify_document_signature(manifest, max_payload_bytes=cap) is False

    def test_the_runtime_parser_keeps_what_the_tool_signs(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A signed entry must survive the consumer's structural parse intact.

        The consumer drops an entry it cannot validate and logs the reason, so a
        field shape this tool signs but the runtime rejects would publish a
        release whose clips silently never appear.
        """
        folder = _publish(release_dir, catalog, key_pair[0])
        parsed = fvm.parse_manifest(_read(folder))
        assert parsed is not None
        assert len(parsed.entries) == 1
        assert parsed.entries[0].bytes == (folder / "monitor-loops.mp4").stat().st_size

    def test_the_canonical_form_sorts_nested_keys(self) -> None:
        """Nested payloads are allowed, so nested key order must not matter."""
        one = manifest_mod.canonical_bytes({"a": [{"y": 1, "x": 2}], "b": 3})
        two = manifest_mod.canonical_bytes({"b": 3, "a": [{"x": 2, "y": 1}]})
        assert one == two
        assert one == b'{"a":[{"x":2,"y":1}],"b":3}\n'


class TestTamperDetection:
    def test_verify_passes_on_a_freshly_produced_folder(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        report = verify_mod.verify_folder(folder, public_key=public)
        assert report["release"] == "0.7.0"
        assert report["entries"] == 1
        assert report["claims_key_id"] is False

    def test_a_flipped_media_byte_is_caught(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        clip = folder / "monitor-loops.mp4"
        raw = bytearray(clip.read_bytes())
        raw[-1] ^= 0xFF
        clip.write_bytes(bytes(raw))
        with pytest.raises(ManifestError, match="hash to"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_an_edited_manifest_field_is_caught(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """The signature covers every top-level field, so any edit breaks it."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        manifest["entries"][0]["title"] = "A title nobody signed"
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="does not verify against the release key"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_a_redirected_cdn_base_is_caught(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        manifest["cdn_base"] = "https://evil.example.com/feature-videos/"
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="does not verify against the release key"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_a_signature_from_another_key_is_refused(
        self,
        release_dir: Path,
        catalog: Path,
        tmp_path: Path,
        key_pair: tuple[Path, Path],
        second_key: Path,
    ) -> None:
        """A well-formed signature over the right bytes, by the wrong key."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        payload = manifest_mod.canonical_bytes(manifest_mod.signed_payload(manifest))
        manifest["signature"] = _sign_bytes(payload, second_key, tmp_path)
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="does not verify against the release key"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_a_stripped_signature_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        del manifest["signature"]
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="missing its signature"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_an_added_top_level_field_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A field outside the schema is refused, not ignored and left unsigned."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        manifest["extra"] = "smuggled"
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="unknown field"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_a_key_id_naming_another_key_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """key_id is optional, but a present one must name the verifying key."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        manifest["key_id"] = f"sha256:{'0' * 64}"
        payload = manifest_mod.canonical_bytes(manifest_mod.signed_payload(manifest))
        manifest["signature"] = _sign_bytes(payload, private, tmp_path)
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="but the verifying key is"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_a_key_id_naming_the_verifying_key_is_accepted(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """The production shape: key_id present, inside the signed payload."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        manifest["key_id"] = manifest_mod.public_key_id(public)
        payload = manifest_mod.canonical_bytes(manifest_mod.signed_payload(manifest))
        manifest["signature"] = _sign_bytes(payload, private, tmp_path)
        _rewrite(folder, manifest)

        report = verify_mod.verify_folder(folder, public_key=public)
        assert report["claims_key_id"] is True

    def test_a_dropped_required_field_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A validly signed but incomplete manifest is still refused.

        Re-signing after dropping a field makes the signature correct over what
        remains, so the schema's required-field set is the only thing left to
        catch it.
        """
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        del manifest["generated_at"]
        payload = manifest_mod.canonical_bytes(manifest_mod.signed_payload(manifest))
        manifest["signature"] = _sign_bytes(payload, private, tmp_path)
        _rewrite(folder, manifest)
        with pytest.raises(ManifestError, match="missing field"):
            verify_mod.verify_folder(folder, public_key=public)

    @pytest.mark.parametrize("name", ["extra.mp4", "SHA256SUMS", "notes.txt"])
    def test_an_unsigned_extra_file_is_caught(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path], name: str
    ) -> None:
        """Every name the manifest does not sign is refused, checksum lists included.

        The manifest is the only record; a checksum file beside it would be one
        unsigned file served from every release prefix, so there is no name the
        stray-file rule carves out.
        """
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        (folder / name).write_bytes(b"not part of this release")
        with pytest.raises(ManifestError, match="unsigned path"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_an_unsigned_file_nested_in_a_subdirectory_is_caught(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """aws s3 sync uploads the whole tree, so a nested file reaches the CDN.

        A top-level-only scan sees a directory, not a file, and passes — which
        would serve unsigned bytes from the release prefix.
        """
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        nested = folder / "assets" / "deep"
        nested.mkdir(parents=True)
        (nested / "payload.js").write_bytes(b"nobody signed this")
        with pytest.raises(ManifestError, match="assets/deep/payload.js"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_republishing_into_a_used_release_folder_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A folder holding a manifest is a release, and a release is immutable.

        A re-recorded clip signed over the same release leaves any consumer that
        cached the old digest unable to validate the new file. The presence of a
        generated file is the whole rule, so there is no content comparison to
        need an exemption for the files this tool writes.
        """
        private, _ = key_pair
        folder = _publish(release_dir, catalog, private)
        published = (folder / "monitor-loops.mp4").read_bytes()

        (folder / "monitor-loops.mp4").write_bytes(published + b"one more frame")
        with pytest.raises(ManifestError, match="never re-signed"):
            _publish(folder, catalog, private)
        # Refused before anything is written: the manifest still describes the
        # bytes it signed, not the edited clip.
        assert _read(folder)["entries"][0]["bytes"] == len(published)

    def test_a_generated_file_is_never_written_through_something_existing(
        self, tmp_path: Path
    ) -> None:
        """The folder check fires first, so this guards the gap after it.

        Nothing end-to-end can reach it -- which is exactly why the primitive is
        exercised directly: it closes a real window between the check and the
        write, unlike a guard that merely restates a structural impossibility.
        """
        occupied = tmp_path / "manifest.json"
        occupied.write_text("keep me\n", encoding="utf-8")

        with pytest.raises(ManifestError, match="already exists"):
            publish_mod._write_new_file(occupied, b"overwritten\n")
        assert occupied.read_text(encoding="utf-8") == "keep me\n"

        fresh = tmp_path / "brand-new.json"
        publish_mod._write_new_file(fresh, b"written\n")
        assert fresh.read_bytes() == b"written\n"

    def test_a_symlinked_manifest_in_the_destination_cannot_be_written_through(
        self, release_dir: Path, tmp_path: Path
    ) -> None:
        """A link planted at manifest.json must not have its target overwritten.

        Exclusive creation is what makes the refusal about the NAME rather than
        about what it resolves to: a plain write would follow the link and
        replace a file outside the release folder entirely.
        """
        victim = tmp_path / "precious.json"
        victim.write_text('{"keep": "me"}\n', encoding="utf-8")
        planted = release_dir / "manifest.json"
        planted.symlink_to(victim)

        with pytest.raises(ManifestError, match="already exists"):
            publish_mod._write_new_file(planted, b"overwritten\n")
        assert victim.read_text(encoding="utf-8") == '{"keep": "me"}\n'

    def test_verification_holds_durations_to_the_publisher_s_rule(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """Both sides must agree on a valid duration, or one blesses what the other bans.

        A bare `> 0` in the verifier admits infinity, which the publisher refuses —
        an asymmetry where a release could pass one side and fail the other.
        """
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        manifest = _read(folder)
        manifest["entries"][0]["duration_s"] = float("inf")
        payload = manifest_mod.canonical_bytes(manifest_mod.signed_payload(manifest))
        manifest["signature"] = _sign_bytes(payload, private, tmp_path)
        _rewrite(folder, manifest)

        with pytest.raises(ManifestError, match="must be finite"):
            verify_mod.verify_folder(folder, public_key=public)

    def test_a_symlink_in_a_release_folder_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """Verification must not follow a link swapped in for signed media."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        real = (folder / "monitor-loops.mp4").read_bytes()
        decoy = tmp_path / "decoy.mp4"
        decoy.write_bytes(real)
        (folder / "monitor-loops.mp4").unlink()
        (folder / "monitor-loops.mp4").symlink_to(decoy)

        with pytest.raises(ManifestError, match="is a symlink"):
            verify_mod.verify_folder(folder, public_key=public)


class TestWindowsIsRefused:
    def test_publishing_is_refused_on_windows_before_anything_is_read(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On Windows the owner-only rule has no answer, so the tool signs nothing.

        Simulated on every platform by pretending to be Windows. The refusal comes
        before the folder is opened, so nothing is written and the message names
        the platforms that work and that `verify.py` still does.
        """
        monkeypatch.setattr(publish_mod.os, "name", "nt")
        with pytest.raises(ManifestError, match="publishing runs on macOS or Linux"):
            publish_mod.main(
                [
                    "--catalog",
                    str(catalog),
                    "--release-dir",
                    str(release_dir),
                    "--cdn-host",
                    "videos.example.com",
                    "--release",
                    "0.7.0",
                    "--signing-key",
                    str(key_pair[0]),
                ]
            )
        assert not (release_dir / "manifest.json").exists()

    def test_the_owner_only_rule_itself_refuses_on_windows(
        self, release_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even called directly, the rule never answers `True` blind on Windows."""
        monkeypatch.setattr(manifest_mod.os, "name", "nt")
        with pytest.raises(ManifestError, match="publishing runs on macOS or Linux"):
            manifest_mod.owner_only(os.stat(release_dir))


class TestOnlyTheOperatorCanChangeWhatIsSigned:
    """The folder, its parent and every clip must be writable by nobody else.

    Signing reads whatever is on disk. If another account can swap the folder
    (writable parent), swap an entry (writable folder) or rewrite a clip (writable
    file), that account decides what gets the release signature. Each is refused
    before anything is signed; the file check is made on the very descriptor
    being hashed.
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_clip_another_user_could_rewrite_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        (release_dir / "monitor-loops.mp4").chmod(0o664)
        with pytest.raises(ManifestError, match="monitor-loops.mp4: another user can write"):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()
        (release_dir / "monitor-loops.mp4").chmod(0o644)
        _publish(release_dir, catalog, key_pair[0])

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_folder_another_user_could_fill_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        release_dir.chmod(0o775)
        with pytest.raises(ManifestError, match="release folder .*: another user can write"):
            _publish(release_dir, catalog, key_pair[0])
        release_dir.chmod(0o755)
        _publish(release_dir, catalog, key_pair[0])

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_folder_another_user_could_swap_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A writable parent lets the whole folder be replaced after the check."""
        tmp_path.chmod(0o777)
        try:
            with pytest.raises(ManifestError, match="its parent folder .*: another user can write"):
                _publish(release_dir, catalog, key_pair[0])
        finally:
            tmp_path.chmod(0o700)
        _publish(release_dir, catalog, key_pair[0])

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_verifier_does_not_demand_ownership(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """Verification is a read; a folder shared read-only with a reviewer still verifies."""
        private, public = key_pair
        folder = _publish(release_dir, catalog, private)
        (folder / "monitor-loops.mp4").chmod(0o664)
        assert verify_mod.verify_folder(folder, public_key=public)


class TestValidationRefusals:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX owner and mode bits")
    def test_a_catalog_another_user_could_rewrite_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """The catalog decides every signed title, doc link and duration.

        A catalog a group member could rewrite is read under the same owner-only
        rule as the media: refused on the descriptor being read, before anything
        is signed or written. A link to a catalog is refused too, so the file
        named on the command line is the file whose owner was judged. The same
        catalog made owner-only is accepted.
        """
        private, _public = key_pair
        catalog.chmod(0o664)
        with pytest.raises(ManifestError, match="another user can write it or owns it"):
            _publish(release_dir, catalog, private)
        assert not (release_dir / "manifest.json").exists()
        catalog.chmod(0o644)
        linked = catalog.parent / "linked-catalog.json"
        linked.symlink_to(catalog)
        with pytest.raises(ManifestError, match="is a symlink"):
            _publish(release_dir, linked, private)
        assert not (release_dir / "manifest.json").exists()
        _publish(release_dir, catalog, private)
        assert (release_dir / "manifest.json").exists()

    def test_a_non_slug_id_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        _rewrite_catalog(catalog, _entry(id="Monitor_Loops"))
        with pytest.raises(ManifestError, match="not a lowercase hyphenated slug"):
            _publish(release_dir, catalog, key_pair[0])

    def test_a_symlinked_clip_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A planted symlink must not be followed and hashed as release media.

        Following it would hash, sign and publish whatever it points at, and the
        signature over those bytes would be perfectly valid — so the refusal has to
        be on the link itself, not on the content it resolves to.
        """
        secret = tmp_path / "not-for-the-cdn.pem"
        secret.write_bytes(b"a local file that must never reach the CDN\n")
        clip = release_dir / "monitor-loops.mp4"
        clip.unlink()
        clip.symlink_to(secret)

        with pytest.raises(ManifestError, match="is a symlink"):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_a_symlinked_poster_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """Both media slots go through the same gate, not just the clip."""
        target = tmp_path / "elsewhere.jpg"
        target.write_bytes(b"\xff\xd8\xff not a release asset")
        poster = release_dir / "monitor-loops.jpg"
        poster.unlink()
        poster.symlink_to(target)

        with pytest.raises(ManifestError, match="is a symlink"):
            _publish(release_dir, catalog, key_pair[0])

    def test_a_fifo_asset_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """Only a regular file can be hashed and signed; a pipe cannot."""
        clip = release_dir / "monitor-loops.mp4"
        clip.unlink()
        try:
            os.mkfifo(clip)
        except (AttributeError, NotImplementedError, OSError):  # pragma: no cover
            pytest.skip("this platform has no mkfifo")
        with pytest.raises(ManifestError, match="not a regular file"):
            _publish(release_dir, catalog, key_pair[0])

    def test_a_missing_poster_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        (release_dir / "monitor-loops.jpg").unlink()
        with pytest.raises(ManifestError, match="is missing"):
            _publish(release_dir, catalog, key_pair[0])

    def test_a_doc_outside_the_tips_allowlist_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        _rewrite_catalog(catalog, _entry(doc="internal-design-note.md"))
        with pytest.raises(ManifestError, match="not in the tips doc allowlist"):
            _publish(release_dir, catalog, key_pair[0])

    def test_an_oversize_clip_is_refused(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clip over the runtime's limit is dropped by every dashboard, so refused here.

        The limit is lowered under the placeholder clip's size rather than a
        65 MiB file being written: the check reads ``RUNTIME_LIMITS`` at call time.
        """
        cap = PLACEHOLDER_CLIP.stat().st_size - 1
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_clip_bytes", cap)
        with pytest.raises(
            ManifestError, match=f"monitor-loops.mp4 is .* over the runtime's {cap} byte clip limit"
        ):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_an_oversize_poster_is_refused_by_its_own_limit(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A poster has its own ceiling: the runtime bounds it lower than a clip."""
        cap = PLACEHOLDER_POSTER.stat().st_size - 1
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_poster_bytes", cap)
        with pytest.raises(
            ManifestError,
            match=f"monitor-loops.jpg is .* over the runtime's {cap} byte poster limit",
        ):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_a_payload_over_the_runtime_s_signed_cap_is_refused(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An over-cap release must fail while publishing, not on a client."""
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_payload_bytes", 200)
        with pytest.raises(ManifestError, match="over the runtime's 200 byte limit"):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_an_oversize_manifest_document_is_refused(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The published file has its own cap: it is indented, the signed bytes are not."""
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_document_bytes", 1000)
        with pytest.raises(
            ManifestError, match="manifest.json is .* over the runtime's 1000 byte limit"
        ):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_too_many_entries_is_refused(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The runtime refuses an over-long list whole, so it must not be published."""
        shutil.copyfile(PLACEHOLDER_CLIP, release_dir / "feature-tips.mp4")
        shutil.copyfile(PLACEHOLDER_POSTER, release_dir / "feature-tips.jpg")
        catalog.write_text(
            json.dumps(
                {
                    "entries": [
                        _entry(),
                        _entry(id="feature-tips", feature="feature-tips", doc="feature-tips.md"),
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        with monkeypatch.context() as patched:
            patched.setitem(manifest_mod.RUNTIME_LIMITS, "max_entries", 1)
            with pytest.raises(ManifestError, match="over the runtime's 1 entry limit"):
                _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

        # The same two-entry catalog signs fine under the real limit.
        folder = _publish(release_dir, catalog, key_pair[0])
        assert len(_read(folder)["entries"]) == 2


class TestReleaseFolder:
    """What the folder itself must look like before anything is signed."""

    def test_a_stray_file_in_the_release_folder_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """`aws s3 sync` uploads the whole folder, so an unnamed file reaches the CDN.

        It would be served from the release prefix under a signature that never
        covered it, and the manifest is the only thing a client checks against.
        """
        (release_dir / "notes.txt").write_text("scratch notes\n", encoding="utf-8")
        with pytest.raises(ManifestError, match="does not name"):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_a_catalog_entry_without_a_duration_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """Nothing here inspects the media, so the catalog is the only source.

        The runtime shows a clip's length from this field, and an entry that omits
        it would publish a clip the dashboard describes as zero seconds long.
        """
        entry = _entry()
        del entry["duration_s"]
        _rewrite_catalog(catalog, entry)
        with pytest.raises(ManifestError, match="duration_s is required"):
            _publish(release_dir, catalog, key_pair[0])

    def test_an_empty_clip_is_refused(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A zero-byte file hashes and signs cleanly, and plays nowhere."""
        (release_dir / "monitor-loops.mp4").write_bytes(b"")
        with pytest.raises(ManifestError, match="is empty"):
            _publish(release_dir, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()

    def test_a_failed_manifest_write_leaves_the_folder_as_assembled(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A disk that fills during the write leaves no manifest.json behind.

        Otherwise the next run would refuse the folder as an existing release. The
        media the operator assembled is untouched either way.
        """
        real_open = open

        def open_then_fail(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = real_open(path, mode, *args, **kwargs)
            if mode == "xb":
                handle.write = lambda data: (_ for _ in ()).throw(  # type: ignore[method-assign]
                    OSError(errno.ENOSPC, "No space left on device")
                )
            return handle

        monkeypatch.setattr("builtins.open", open_then_fail)
        with pytest.raises(OSError, match="No space left"):
            _publish(release_dir, catalog, key_pair[0])
        assert sorted(p.name for p in release_dir.iterdir()) == [
            "monitor-loops.jpg",
            "monitor-loops.mp4",
        ]

    def test_media_that_changes_while_being_read_is_refused(
        self, release_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file still being written must not be signed.

        The clip grows after its size is taken and before the read reaches
        end-of-file -- an encoder not yet finished. The digest would then cover
        more bytes than the declared size and no verifier would accept the
        release, so the read is refused. Once the file holds still it hashes.
        """
        clip = release_dir / "monitor-loops.mp4"
        real_fstat = os.fstat
        grown = []

        def fstat_then_grow(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            if not grown:
                grown.append(True)
                with clip.open("ab") as handle:
                    handle.write(b"\0" * 64)
            return info

        monkeypatch.setattr(os, "fstat", fstat_then_grow)
        with pytest.raises(ManifestError, match="changed while it was being read"):
            manifest_mod.hash_regular_file(clip, where="test")
        monkeypatch.undo()
        digest, size = manifest_mod.hash_regular_file(clip, where="test")
        assert size == clip.stat().st_size == PLACEHOLDER_CLIP.stat().st_size + 64
        assert digest == hashlib.sha256(clip.read_bytes()).hexdigest()

    def test_a_write_that_fails_after_creating_the_file_removes_it(self, tmp_path: Path) -> None:
        """A partial generated file is worse than none.

        The exclusive open succeeds and the write then fails (here with a payload
        the handle cannot take, standing in for a full disk); the name must not be
        left behind holding part of the data.
        """
        target = tmp_path / "manifest.json"
        with pytest.raises(TypeError):
            publish_mod._write_new_file(target, None)  # type: ignore[arg-type]
        assert not target.exists()

    def test_the_refusal_of_a_half_release_names_what_to_delete(
        self, release_dir: Path, catalog: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """An interrupted run's leftover is one known file, and the message says so."""
        (release_dir / "manifest.json").write_bytes(b"{")
        with pytest.raises(ManifestError, match="delete manifest.json and run it again"):
            _publish(release_dir, catalog, key_pair[0])

    def test_the_upload_plan_names_the_real_folder_not_a_path_through_a_link(
        self,
        release_dir: Path,
        catalog: Path,
        tmp_path: Path,
        key_pair: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A folder reached through a linked ancestor is signed and printed by its real path.

        The upload plan is pasted into a shell later, by hand. Were it to carry the
        path the operator typed, an ancestor link in it could be re-pointed between
        signing and `aws s3 sync`, and the sync would upload whatever the link then
        named. The tool resolves the folder once and names only that real path.
        """
        via = tmp_path / "via"
        via.symlink_to(release_dir.parent, target_is_directory=True)
        through_link = via / release_dir.name
        _publish(through_link, catalog, key_pair[0])
        out = capsys.readouterr().out
        real = os.path.realpath(release_dir)
        assert f"aws s3 sync {shlex.quote(real)}/ " in out
        assert f"signed {real}" in out
        assert str(through_link) not in out
        assert (release_dir / "manifest.json").exists()

    def test_a_symlinked_release_folder_is_refused(
        self, release_dir: Path, catalog: Path, tmp_path: Path, key_pair: tuple[Path, Path]
    ) -> None:
        """A link at the folder path must be refused, not followed and written into.

        The tool writes two files into whatever it is handed, so a link here would
        put a signed manifest somewhere the operator never named.
        """
        link = tmp_path / "release-link"
        link.symlink_to(release_dir, target_is_directory=True)
        with pytest.raises(ManifestError, match="not a directory"):
            _publish(link, catalog, key_pair[0])
        assert not (release_dir / "manifest.json").exists()
