#!/usr/bin/env python3
"""Sign a feature-videos release folder for the CDN, in place.

The operator assembles the folder: ``dist/feature-videos/<release>/`` holding an
``<id>.mp4`` and an ``<id>.jpg`` for every entry in a ``catalog.json`` kept
beside it. This tool reads that folder, checks every file against the catalog,
hashes it, signs the result and writes ``manifest.json`` into
the same folder. It copies nothing and it never uploads: it prints the exact
``aws s3 sync --dryrun`` and CloudFront invalidation commands and stops, so the
credentials that can write to a public origin stay with the human who owns them.

Signing reuses the CLI artifact manifest's trust root: same key, same
``RSASSA_PKCS1_V1_5_SHA_256``, same canonical-JSON bytes. Keys are separated by
purpose. Production signs with ``--kms-key-arn``, where the private half is a
non-exportable AWS KMS key that no human can read and the manifest records
``key_id`` as the hint that it was used. ``--signing-key`` takes a local private
key for staging and tests, omits ``key_id``, and says out loud that the result
is not a production artifact. Either way openssl verifies the signature before
anything reaches disk, so an unverifiable folder is never produced.

Usage:

    python3 scripts/feature-videos/publish.py \\
        --catalog catalog.json --cdn-host videos.example.com --kms-key-arn <arn>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `kiro_crew.tips_allowlist` is dependency-free (a frozenset and nothing else), so
# importing it from a bare checkout never pulls the runtime -- the same import
# `scripts/generate_tips_catalog.py` makes for the same list.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from _manifest import (  # noqa: E402
    PUBLIC_KEY_PATH,
    SCHEMA,
    WINDOWS_PUBLISH_REFUSAL,
    ManifestError,
    canonical_bytes,
    check_document_size,
    check_entry_count,
    check_media_size,
    check_owner_only_dir,
    check_signable,
    hash_regular_file,
    kms_sign_digest,
    load_json_object,
    parse_generated_at,
    public_key_der,
    public_key_id,
    require_text,
    run_openssl,
    validate_cdn_base,
    validate_cdn_host,
    validate_doc,
    validate_duration,
    validate_release,
    validate_slug,
    verify_signature,
)

from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Ceiling on ``catalog.json`` itself. A release ships a handful of clips, so a
#: file past this is a mistake rather than a large catalog.
_MAX_CATALOG_BYTES = 256 * 1024

#: A floor is a bare release, matching what the runtime's version compare reads.
_MIN_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")


def _warn(message: str) -> None:
    print(f"publish: warning: {message}", file=sys.stderr)


def _repo_version() -> str:
    """The version in ``pyproject.toml``, used when ``--release`` is omitted."""
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise ManifestError("could not read version from pyproject.toml; pass --release")
    return match.group(1)


def _validate_catalog_entry(
    raw: Any, index: int, seen: set[str], allowlist: frozenset[str]
) -> dict[str, Any]:
    where = f"catalog entry {index}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: must be a JSON object")
    unknown = set(raw) - {
        "id",
        "feature",
        "title",
        "description",
        "doc",
        "used_when",
        "min_version",
        "duration_s",
    }
    if unknown:
        raise ManifestError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")

    entry_id = validate_slug(require_text(raw, "id", where=where), where=where)
    if entry_id in seen:
        raise ManifestError(f"{where}: duplicate id {entry_id!r}")
    seen.add(entry_id)

    entry: dict[str, Any] = {
        "id": entry_id,
        "feature": require_text(raw, "feature", where=where),
        "title": require_text(raw, "title", where=where),
        "description": require_text(raw, "description", where=where),
        "doc": validate_doc(
            require_text(raw, "doc", where=where), where=where, allowlist=allowlist
        ),
    }

    used_when = raw.get("used_when", [])
    if not isinstance(used_when, list) or not all(
        isinstance(signal, str) and signal and len(signal) <= 200 for signal in used_when
    ):
        raise ManifestError(f"{where}: used_when must be a list of non-empty strings")
    # Signal NAMES are not checked against the runtime's probe registry: doing
    # so would mean executing runtime code from a publishing tool, and a second
    # copy of the registry here would drift from its answer. The runtime treats
    # an unregistered signal as "feature not used" and logs it, so a typo shows
    # the clip rather than hiding it.
    entry["used_when"] = list(used_when)

    min_version = raw.get("min_version", "")
    if not isinstance(min_version, str):
        raise ManifestError(f"{where}: min_version must be a string")
    if min_version and _MIN_VERSION_RE.fullmatch(min_version) is None:
        raise ManifestError(f"{where}: min_version must be a bare release like 0.7.0")
    entry["min_version"] = min_version

    # Required: nothing here inspects the media, so the catalog is the only
    # source of a duration, and the runtime shows a clip's length from it.
    if "duration_s" not in raw:
        raise ManifestError(f"{where}: duration_s is required")
    entry["duration_s"] = round(validate_duration(raw["duration_s"], where=where), 3)
    return entry


def load_catalog(path: Path) -> list[dict[str, Any]]:
    # Every title, description, doc link and duration in the release comes from
    # this file, so it is read under the same rule as the media: through one
    # descriptor, a regular file nobody but the operator (or root) can write, and
    # unchanged across the read. A catalog another account could rewrite would let
    # that account choose what gets the release signature.
    document = load_json_object(path, limit=_MAX_CATALOG_BYTES, require_owner_only=True)
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"{path.name} must carry a non-empty 'entries' array")
    seen: set[str] = set()
    return [
        _validate_catalog_entry(raw, index, seen, TIP_DOC_ALLOWLIST)
        for index, raw in enumerate(entries)
    ]


def check_release_dir(release_dir: Path, catalog: list[dict[str, Any]]) -> None:
    """Refuse a folder that is not exactly the catalog's media, and nothing else.

    Three rules, all cheap. The folder must be a real directory. It must not
    already hold a generated file: a release is immutable, so a folder with a
    ``manifest.json`` in it is a release someone may already be serving, and
    changing a clip means cutting a new release rather than re-signing this one.
    And it must hold no file the catalog does not name: ``aws s3 sync`` uploads
    the whole tree, so a stray file here would be served from the release prefix
    under a signature that never covered it. Refusing here is what lets the
    operator fix the folder before anything is signed.
    """
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise ManifestError(f"release folder is not a directory: {release_dir}")
    # Nobody but the operator (or root) may be able to swap this folder or an entry
    # in it: what is signed is whatever is on disk when it is read, and a folder or
    # parent another account can write is one where that can change under the tool.
    check_owner_only_dir(release_dir, where="release folder")
    expected = {f"{entry['id']}.mp4" for entry in catalog} | {
        f"{entry['id']}.jpg" for entry in catalog
    }
    present = {item.name for item in release_dir.iterdir()}
    if "manifest.json" in present:
        raise ManifestError(
            f"{release_dir} already holds manifest.json: it is a release, and a "
            "release is never re-signed. Cut a new release. If an earlier run of this tool was "
            "interrupted and nothing was uploaded, delete manifest.json and run it again."
        )
    stray = sorted(present - expected)
    if stray:
        raise ManifestError(
            f"{release_dir} holds file(s) the catalog does not name: {', '.join(stray[:5])}"
            f"{' and more' if len(stray) > 5 else ''}. A release folder carries only the "
            "media the manifest signs."
        )
    missing = sorted(expected - present)
    if missing:
        raise ManifestError(f"{release_dir} is missing: {', '.join(missing[:5])}")


def build_entries(catalog: list[dict[str, Any]], release_dir: Path) -> list[dict[str, Any]]:
    """Hash every entry's media in place and return the manifest entries."""
    built: list[dict[str, Any]] = []
    for entry in catalog:
        clip_name = f"{entry['id']}.mp4"
        poster_name = f"{entry['id']}.jpg"
        clip_sha, clip_size = hash_regular_file(
            release_dir / clip_name, where="release folder", require_owner_only=True
        )
        poster_sha, poster_size = hash_regular_file(
            release_dir / poster_name, where="release folder", require_owner_only=True
        )
        check_media_size(clip_name, clip_size, kind="clip")
        check_media_size(poster_name, poster_size, kind="poster")
        built.append(
            {
                "id": entry["id"],
                "feature": entry["feature"],
                "title": entry["title"],
                "description": entry["description"],
                "file": clip_name,
                "poster": poster_name,
                "sha256": clip_sha,
                "poster_sha256": poster_sha,
                "bytes": clip_size,
                "duration_s": entry["duration_s"],
                "doc": entry["doc"],
                "used_when": entry["used_when"],
                "min_version": entry["min_version"],
            }
        )
    return built


def _sign_with_kms(payload: bytes, key_arn: str) -> bytes:
    """Sign *payload* with the release KMS key, through the CLI signer's own flow.

    ``kms_sign_digest`` checks that the KMS key's public half byte-matches the
    committed one before signing; without that a mistyped ARN would sign with
    some other key and produce a folder every dashboard silently refuses.
    """
    return kms_sign_digest(
        key_arn, public_key_der(PUBLIC_KEY_PATH), hashlib.sha256(payload).digest()
    )


def _sign_with_key(payload: bytes, private_key: Path, scratch: Path) -> bytes:
    """Sign *payload* with a local private key, for staging and tests."""
    if not private_key.is_file():
        raise ManifestError(f"signing key is missing: {private_key}")
    payload_path = scratch / "payload.json"
    payload_path.write_bytes(payload)
    return run_openssl(["dgst", "-sha256", "-sign", str(private_key), str(payload_path)])


def sign_document(
    document: dict[str, Any],
    *,
    scratch: Path,
    signing_key: Path | None,
    kms_key_arn: str | None,
) -> dict[str, Any]:
    """Return *document* with ``key_id`` where applicable and a verified signature.

    Verification is not a courtesy: the signature is checked against the public
    half before the manifest is assembled, so a folder that reaches disk is one
    whose signature openssl already accepted over exactly these bytes.
    """
    if signing_key is not None:
        public_key = scratch / "public.pem"
        run_openssl(["pkey", "-in", str(signing_key), "-pubout", "-out", str(public_key)])
        # key_id is omitted for a local key. It is a hint about WHICH pinned key
        # signed, and a staging key is not one — claiming an id the runtime does
        # not pin would be a false hint, and claiming the pinned one would be a lie.
        signed = dict(document)
    else:
        public_key = PUBLIC_KEY_PATH
        signed = {**document, "key_id": public_key_id(PUBLIC_KEY_PATH)}

    payload = check_signable(signed)
    if signing_key is not None:
        signature = _sign_with_key(payload, signing_key, scratch)
    elif kms_key_arn:
        signature = _sign_with_kms(payload, kms_key_arn)
    else:  # pragma: no cover - argparse requires one of the two
        raise ManifestError("no signing method given")
    if not signature:
        raise ManifestError("signing produced no signature")

    manifest = {**signed, "signature": base64.b64encode(signature).decode("ascii")}
    verify_signature(manifest, public_key=public_key)
    return manifest


def _write_new_file(path: Path, data: bytes) -> None:
    """Create *path* and write *data*, refusing to write through anything existing.

    Exclusive creation is the point. The folder was checked to hold no generated
    file, and ``"x"`` keeps that true even if one appears in the gap: a plain
    write would follow a symlink planted at the name and overwrite its target.
    """
    try:
        handle = open(path, "xb")
    except FileExistsError as exc:
        raise ManifestError(f"{path.name} already exists in the release folder") from exc
    try:
        with handle:
            handle.write(data)
    except BaseException:
        # The name became this run's the moment the exclusive open succeeded, and
        # a file holding part of the data (disk full, interrupt) is worse than
        # none: the next run would refuse the folder as an existing release.
        _unlink_quietly(path)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_manifest(release_dir: Path, manifest: dict[str, Any]) -> None:
    """Write ``manifest.json`` beside the media it describes.

    The manifest is the whole release record: every digest and size is inside
    the signed document, so nothing else is written. A write that fails part-way
    removes the file it created (``_write_new_file``), so the folder is left as
    the operator assembled it rather than as a half-release the next run refuses.
    """
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    check_document_size(manifest_bytes)
    _write_new_file(release_dir / "manifest.json", manifest_bytes)


def _print_upload_plan(release_dir: Path, release: str) -> None:
    prefix = f"feature-videos/{release}/"
    target = f"s3://<BUCKET>/{prefix}"
    # The folder is pasted into a shell: a space or a quote in its path must not split
    # the command or change what is uploaded.
    folder = shlex.quote(str(release_dir))
    print()
    print("Nothing was uploaded. Run these yourself, in this order:")
    print()
    print(f"  aws s3 sync --dryrun {folder}/ {target}")
    print(f"  aws s3 sync {folder}/ {target}")
    print(
        f"  aws cloudfront create-invalidation --distribution-id <DISTRIBUTION_ID> "
        f"--paths '/{prefix}*'"
    )
    print()
    print(f"Verify the folder first: python3 scripts/feature-videos/verify.py {folder}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign a feature-videos release folder in place.")
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="catalog.json describing the entries; kept OUTSIDE the release folder",
    )
    parser.add_argument(
        "--release-dir",
        type=Path,
        help=(
            "the folder holding <id>.mp4 and <id>.jpg per entry; manifest.json is "
            "written into it (default dist/feature-videos/<release>)"
        ),
    )
    parser.add_argument(
        "--cdn-host",
        required=True,
        help="CDN host serving the release, e.g. videos.example.com; a host name, no path",
    )
    parser.add_argument("--release", help="release version; defaults to pyproject.toml's version")
    signing = parser.add_mutually_exclusive_group(required=True)
    signing.add_argument(
        "--kms-key-arn", help="release KMS key ARN; the production path, key never leaves KMS"
    )
    signing.add_argument(
        "--signing-key",
        type=Path,
        help="local RSA private key; for staging and tests, not for a public release",
    )
    args = parser.parse_args(argv)

    # Fail closed where the owner-only rule cannot be judged. Nothing is read,
    # hashed or signed on Windows; `verify.py` still runs there.
    if os.name == "nt":
        raise ManifestError(WINDOWS_PUBLISH_REFUSAL)

    release = validate_release(args.release or _repo_version())
    # The base names the CDN's feature-videos ROOT, not this release's folder:
    # the runtime builds every asset URL as ``<cdn_base>/<release>/<name>``
    # (``VideoManifest.asset_url``), so a base already carrying the release would
    # double it in every URL and no clip would load. The host is a bare host for
    # the same reason: a path in it would be signed into every URL while the
    # upload plan below names the bucket root.
    cdn_base = validate_cdn_base(f"https://{validate_cdn_host(args.cdn_host)}/feature-videos/")
    generated_at = parse_generated_at(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    # The folder is named once, canonically. The path the operator gave is checked
    # (a link at the leaf is refused, never followed into), then resolved, and that
    # one real path is what is validated, signed into, written to and printed in the
    # upload plan -- so no component of the pasted `aws s3 sync` command is a link
    # that could be re-pointed between signing and the operator's upload.
    given = args.release_dir or (_REPO_ROOT / "dist" / "feature-videos" / release)
    if given.is_symlink():
        raise ManifestError(f"release folder is not a directory: {given}")
    release_dir = Path(os.path.realpath(given))

    catalog = load_catalog(args.catalog)
    check_entry_count(len(catalog))
    check_release_dir(release_dir, catalog)
    entries = build_entries(catalog, release_dir)

    document: dict[str, Any] = {
        "schema": SCHEMA,
        "release": release,
        "cdn_base": cdn_base,
        "generated_at": generated_at,
        "entries": entries,
    }
    with tempfile.TemporaryDirectory(prefix="feature-videos-sign-") as scratch:
        manifest = sign_document(
            document,
            scratch=Path(scratch),
            signing_key=args.signing_key,
            kms_key_arn=args.kms_key_arn,
        )
    write_manifest(release_dir, manifest)

    payload_bytes = len(canonical_bytes({k: v for k, v in manifest.items() if k != "signature"}))
    print(f"signed {release_dir}")
    print(f"  {len(entries)} entry/entries, signed payload {payload_bytes} bytes")
    if args.signing_key is not None:
        _warn(
            "signed with a local key and no key_id: this is a staging artifact. "
            "A production release is signed with --kms-key-arn."
        )
    else:
        print(f"  key_id {manifest['key_id']}")
    _print_upload_plan(release_dir, release)
    return 0


if __name__ == "__main__":
    # OSError too: a full disk or an unreadable folder is a refusal with a
    # reason, not a traceback -- the same exit the contract violations take.
    try:
        raise SystemExit(main())
    except (ManifestError, OSError) as exc:
        print(f"publish: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
