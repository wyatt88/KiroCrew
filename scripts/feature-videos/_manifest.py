"""Shared contract for the feature-videos release manifest.

The manifest is a signed document: ``signature`` is base64 at the top level and
covers canonical JSON of every other top-level field. Nested values are fine —
the canonical encoding sorts nested keys too, so publisher and verifier agree on
the bytes either way.

The signing plumbing is the CLI artifact manifest's own, loaded from
``packaging/signing/cli-manifest.py`` by path: the canonical-JSON rule, the
key-id derivation, the openssl and AWS CLI runners and the pinned KMS flow. That
file is not importable by name (a hyphen) and is not the runtime, so a publishing
tool that runs from a bare checkout can share it without executing the code it
produces input for. Nothing signing-related is restated here, so nothing here can
drift from the signer the release workflow already uses.

One trust root and one algorithm, both the CLI artifact manifest's: a release
signs feature videos with the same offline key ``cli.sh`` pins, so no consumer
needs a second key to trust. Signing keys are separated by purpose — the
production key lives in AWS KMS and is never exportable, and a local key file is
for staging and tests only.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_MANIFEST_SIGNER = _REPO_ROOT / "packaging" / "signing" / "cli-manifest.py"


def _load_cli_manifest_signer() -> ModuleType:
    """The CLI artifact manifest signer, loaded by path.

    Its filename carries a hyphen, so it cannot be imported by name; loading it
    through a spec is the one way to share its code rather than copy it.
    """
    spec = importlib.util.spec_from_file_location(
        "kirocrew_cli_manifest_signer", _CLI_MANIFEST_SIGNER
    )
    if spec is None or spec.loader is None:  # pragma: no cover - present in every checkout
        raise RuntimeError(f"cannot load {_CLI_MANIFEST_SIGNER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_signer = _load_cli_manifest_signer()

#: One error class for both tools: a caller catching the signer's contract
#: violations catches this tool's too, because they are the same class.
ManifestError = _signer.ManifestError

#: Shared with the CLI signer, not restated: the algorithm KMS names (``openssl
#: dgst -sha256`` produces the same bytes for the local-key path), the runners,
#: and the key identity a consumer pins.
ALGORITHM: str = _signer.ALGORITHM
run_openssl = _signer.run_openssl
public_key_der = _signer.public_key_der
public_key_id = _signer.public_key_id
kms_sign_digest = _signer.kms_sign_digest
MAX_SIGNATURE_BYTES: int = _signer.MAX_SIGNATURE_BYTES

SCHEMA = "kirocrew-feature-videos-manifest-v1"

#: The committed public half of the release signing key — the same PEM the
#: runtime pins and ``cli.sh`` embeds. Read from the file rather than restated as
#: a constant, so there is nothing here that can drift from the trust root.
PUBLIC_KEY_PATH = _REPO_ROOT / "packaging" / "signing" / "cli-manifest-public.pem"

#: Top-level fields a manifest carries. ``key_id`` is optional: the key is
#: PINNED, so ``key_id`` never established trust — it is a publisher-side hint
#: about which key signed, and the runtime requires it to match only when
#: present.
REQUIRED_FIELDS = ("schema", "release", "cdn_base", "generated_at", "entries")
OPTIONAL_FIELDS = ("key_id",)

#: The runtime's own hard limits, and this tool's: a release over any of them is
#: refused whole (payload, document, entries) or has its media dropped (clip,
#: poster) by every dashboard, so publishing refuses it first, while a person is
#: watching. There is no flag to loosen one, because a looser publisher would
#: only move the failure to where the operator cannot see it.
#: ``test_the_runtime_limits_are_the_consumer_s`` pins each number to the
#: consumer constant named beside it.
RUNTIME_LIMITS: dict[str, int] = {
    "max_payload_bytes": 256 * 1024,  # feature_videos_manifest._SIGNED_PAYLOAD_MAX_BYTES
    "max_document_bytes": 1024 * 1024,  # feature_videos_manifest._MANIFEST_MAX_BYTES
    "max_entries": 1000,  # feature_videos_manifest._MAX_ENTRIES
    "max_clip_bytes": 64 * 1024 * 1024,  # feature_videos_manifest._MAX_ENTRY_BYTES
    "max_poster_bytes": 8 * 1024 * 1024,  # feature_videos_cache.MAX_POSTER_BYTES
    "max_duration_s": 3600,  # feature_videos_manifest._MAX_DURATION_S
}

#: The runtime refuses a media basename longer than this
#: (``feature_videos_manifest._SAFE_BASENAME_RE``), and ``<id>.mp4`` must fit.
RUNTIME_MAX_BASENAME_CHARS = 96

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
#: A release is exactly ``major.minor.patch``, each component a number with no
#: leading zero and up to five digits. The rule is the runtime's own round trip:
#: ``feature_videos_manifest.running_release`` parses each component with
#: ``int()`` and formats the folder it asks for as ``f"{major}.{minor}.{patch}"``,
#: so the only folder a dashboard ever requests is the one whose name survives
#: that trip unchanged. ``1.2/`` (padded to ``1.2.0``), ``1.2.3.4`` (truncated) and
#: ``00.7.0`` (asked for as ``0.7.0``) would each be signed, uploaded and never
#: fetched. The CDN path segment is built from the value, so anything needing
#: escaping is refused rather than quoted.
_RELEASE_COMPONENT = r"(?:0|[1-9][0-9]{0,4})"
_RELEASE_RE = re.compile(rf"{_RELEASE_COMPONENT}(?:\.{_RELEASE_COMPONENT}){{2}}\Z")
#: An id becomes the basename ``<id>.mp4`` (and ``<id>.jpg``), which must pass
#: the runtime's basename bound, so the id is bounded by that less the suffix.
MAX_ID_CHARS = RUNTIME_MAX_BASENAME_CHARS - len(".mp4")
_MAX_TEXT_CHARS = 2048


def canonical_bytes(value: dict[str, Any]) -> bytes:
    """The exact byte form both signer and verifier hash.

    The CLI signer's rule, applied to a document with nested values (its own
    documents are flat, so it never meets these errors): a payload the encoder
    cannot serialize, or one nested past the recursion limit, is a refusal with a
    reason rather than a traceback.
    """
    try:
        return _signer.canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManifestError(f"manifest payload cannot be canonicalized: {exc}") from exc


def signed_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Every top-level field the signature covers, i.e. all but ``signature``."""
    return {key: value for key, value in manifest.items() if key != "signature"}


def require_text(mapping: dict[str, Any], key: str, *, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT_CHARS:
        raise ManifestError(f"{where}: field {key!r} must be non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ManifestError(f"{where}: field {key!r} must not carry control characters")
    return value


def validate_slug(value: str, *, where: str) -> str:
    if _SLUG_RE.fullmatch(value) is None:
        raise ManifestError(f"{where}: id {value!r} is not a lowercase hyphenated slug")
    if len(value) > MAX_ID_CHARS:
        raise ManifestError(f"{where}: id {value!r} is longer than {MAX_ID_CHARS} characters")
    return value


#: ``O_NOFOLLOW`` makes the kernel refuse a symlink at open time, which is the
#: only way to check and read the same file. Absent on Windows, where the lstat
#: check below is the best available and is documented as such.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: A FIFO opened read-only BLOCKS until a writer appears, so opening one would
#: hang the tool before any check could refuse it. Non-blocking makes the open
#: return so ``fstat`` can reject it; on a regular file it changes nothing.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


WINDOWS_PUBLISH_REFUSAL = (
    "publishing runs on macOS or Linux: on Windows the file owner and mode bits carry "
    "no information, so this tool cannot tell whether another account could rewrite "
    "the media it is about to sign, and it refuses rather than sign blind; verify.py "
    "works everywhere"
)


def owner_only(info: os.stat_result) -> bool:
    """Whether the object *info* describes can be changed by nobody but root or us.

    A group or other write bit, or an owner who is neither root nor the caller,
    means another account can rewrite the object (a file) or swap its entries (a
    directory). Signing runs on whatever is on disk at read time, so the folder
    being signed, the folder that names it and every clip in it must all pass --
    then the only account that can change what is signed between the check and
    the write is the operator's own.

    On Windows ``os.stat`` reports ``st_uid == 0`` and ``st_mode == 0o777`` for
    every path, so the question has no answer here and the tool refuses instead
    of guessing; `publish.py` refuses earlier still, before anything is read.
    """
    if os.name == "nt":
        raise ManifestError(WINDOWS_PUBLISH_REFUSAL)
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return info.st_uid in (0, os.getuid())


_OWNER_ONLY_HINT = (
    "another user can write it or owns it; a release is signed only from a folder "
    "nobody but you (or root) can change -- fix its owner and permissions"
)


def check_owner_only_dir(directory: Path, *, where: str) -> None:
    """Refuse a folder another account could swap or fill: it and its parent.

    A writable parent lets the whole folder be replaced between the check and
    the read; a writable folder lets a single entry be replaced. Both are read
    from the real path, so a link into a writable place is caught too.
    """
    real = Path(os.path.realpath(directory))
    for target, label in ((real, "release folder"), (real.parent, "its parent folder")):
        if not owner_only(os.stat(target)):
            raise ManifestError(f"{where}: {label} {target}: {_OWNER_ONLY_HINT}")


def hash_regular_file(
    path: Path, *, where: str, require_owner_only: bool = False
) -> tuple[str, int]:
    """The sha256 and size of *path*, read through one descriptor.

    Refuses a symlink, directory or device at open time rather than after a
    separate stat, so the digest and the size describe the same regular file. The
    name is always ``<id>.mp4``, ``<id>.jpg`` or ``manifest.json`` inside a folder
    the operator assembled, so it cannot traverse; what this closes is a link or a
    special file planted at that leaf.

    The file must also hold still while it is read: the digest covers every byte
    read to end-of-file, and the size is what the manifest declares, so a file
    still being written (an encoder not yet finished, a copy still in flight)
    would sign a digest over more bytes than the declared size and produce a
    release no verifier accepts. Bytes hashed must equal the size, and the size
    and modification time must be the same before and after the read.

    With *require_owner_only*, the file must also be one nobody but root or the
    caller can write, judged on the very descriptor being hashed: a clip a group
    member could rewrite is not signed at all, rather than signed and then
    trusted to hold still.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ManifestError(
                f"{where}: {path.name} is a symlink; release media must be a regular file"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise ManifestError(f"{where}: missing asset: {path.name}") from exc
        raise ManifestError(f"{where}: cannot read {path.name}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError(f"{where}: {path.name} is not a regular file")
        if require_owner_only and not owner_only(info):
            raise ManifestError(f"{where}: {path.name}: {_OWNER_ONLY_HINT}")
        if not _O_NOFOLLOW and path.is_symlink():  # pragma: no cover - Windows only
            raise ManifestError(
                f"{where}: {path.name} is a symlink; release media must be a regular file"
            )
        digest = hashlib.sha256()
        hashed = 0
        # ``os.fdopen`` in binary mode reads raw bytes on every platform; Windows
        # would otherwise translate line endings and hash bytes that are not on disk.
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                hashed += len(chunk)
            after = os.fstat(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if (
        hashed != info.st_size
        or after.st_size != info.st_size
        or after.st_mtime_ns != info.st_mtime_ns
    ):
        raise ManifestError(
            f"{where}: {path.name} changed while it was being read; a release is signed "
            "only over media that has finished being written"
        )
    return digest.hexdigest(), info.st_size


def validate_duration(value: object, *, where: str) -> float:
    """A positive, finite duration.

    ``math.isfinite`` is the load-bearing half. A bare ``> 0`` admits infinity,
    and ``json.dumps`` renders that as the bare token ``Infinity`` — which is
    not JSON, so the signed bytes would be a document a strict parser refuses
    while the signature over them verifies perfectly.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{where}: duration_s must be a number")
    try:
        # A JSON integer has no width limit, and float() on a 400-digit one raises
        # OverflowError -- which is neither ValueError nor TypeError, so it would
        # escape as a traceback instead of a refusal.
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ManifestError(f"{where}: duration_s is out of range") from exc
    if not math.isfinite(number):
        raise ManifestError(f"{where}: duration_s must be finite, not {value!r}")
    # Round FIRST, then validate: the rounded value is what gets signed, and a
    # duration under 0.0005 rounds to 0.0 -- which this tool's own verifier
    # refuses, so publishing it produces a folder nobody can validate.
    rounded = round(number, 3)
    if not rounded > 0:
        raise ManifestError(
            f"{where}: duration_s must be positive after rounding to milliseconds, "
            f"not {value!r}"
        )
    # The runtime reads any duration over its ceiling as 0.0 ("unknown"), so a
    # longer one would sign cleanly and display as nothing.
    ceiling = RUNTIME_LIMITS["max_duration_s"]
    if rounded > ceiling:
        raise ManifestError(
            f"{where}: duration_s {value!r} is over the runtime's {ceiling} second ceiling; "
            "every dashboard would show it as unknown"
        )
    return rounded


def validate_release(value: str) -> str:
    if _RELEASE_RE.fullmatch(value) is None:
        raise ManifestError(
            f"release {value!r} must be a three-component numeric version like 0.7.0, each "
            "component up to five digits with no leading zero: that is the only folder "
            "shape a dashboard asks for"
        )
    return value


def validate_key_id(value: str, *, where: str) -> str:
    if not value.startswith("sha256:") or _SHA256_RE.fullmatch(value[len("sha256:") :]) is None:
        raise ManifestError(f"{where}: key_id must be 'sha256:' followed by 64 hex characters")
    return value


def validate_doc(value: str, *, where: str, allowlist: frozenset[str]) -> str:
    """A video's doc must be a user-facing feature doc, the gate tips use.

    The allowlist is passed in rather than read here so the caller owns where it
    comes from; sharing the runtime's list is what stops a clip pointing at an
    internal design note, which a second copy of the list would eventually let
    through.
    """
    if value not in allowlist:
        raise ManifestError(f"{where}: doc {value!r} is not in the tips doc allowlist")
    return value


def validate_cdn_base(value: str) -> str:
    """An HTTPS directory URL with a trailing slash and nothing to strip.

    Query, fragment and userinfo are refused rather than dropped: the value is
    concatenated with a filename by every consumer, and a base carrying any of
    them would build a URL none of them agree on.
    """
    try:
        parsed = urlsplit(value)
        # `port` is parsed lazily and raises on `:abc` or a value past 65535; read it
        # here so a malformed port is refused now, not by every dashboard's download.
        port = parsed.port
    except ValueError as exc:
        raise ManifestError(f"cdn_base is not a well-formed URL: {exc}") from exc
    if port == 0:
        raise ManifestError("cdn_base is not a well-formed URL: port 0 cannot be connected to")
    if parsed.scheme != "https":
        raise ManifestError("cdn_base must be an https URL")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ManifestError("cdn_base must name a host and carry no credentials")
    if parsed.query or parsed.fragment:
        raise ManifestError("cdn_base must carry no query string or fragment")
    if not value.endswith("/"):
        raise ManifestError("cdn_base must end with a slash")
    if "//" in parsed.path or ".." in parsed.path:
        raise ManifestError("cdn_base path must not carry an empty segment or a traversal")
    return value


_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _is_dns_name_or_ip(hostname: str) -> bool:
    """A DNS host name (RFC 1123 labels, 253 characters at most) or an IP literal."""
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if len(hostname) > 253:
        return False
    labels = hostname.split(".")
    return all(_DNS_LABEL_RE.fullmatch(label) for label in labels)


def validate_cdn_host(value: str) -> str:
    """A DNS name or IP literal with an optional port: what ``--cdn-host`` may carry.

    The publisher builds ``cdn_base`` as ``https://<host>/feature-videos/`` and
    prints an upload plan for ``s3://<BUCKET>/feature-videos/<release>/``. A
    value carrying a path (``videos.example.com/archive``) would be signed into
    every asset URL while the plan uploads to the root, so every request from a
    dashboard would miss. Credentials, a query or a fragment are refused for the
    same reason ``validate_cdn_base`` refuses them. And the host itself must be
    one a URL library will connect to: ``urlsplit`` tolerates a space or a
    stray character inside the authority, and a host with one would be signed
    into every asset URL and fail every download with ``InvalidURL``.
    """
    try:
        parsed = urlsplit(f"https://{value}/")
        port = parsed.port
    except ValueError as exc:
        raise ManifestError(f"--cdn-host is not a well-formed host: {exc}") from exc
    # Port 0 parses but no client connects to it; it would be signed into every URL.
    if (
        parsed.netloc != value
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        raise ManifestError(
            "--cdn-host must be a host name with an optional port, like "
            "videos.example.com, and carry no path, credentials, query or fragment"
        )
    if not _is_dns_name_or_ip(parsed.hostname):
        raise ManifestError(
            f"--cdn-host {value!r} is not a DNS host name or an IP address: labels are "
            "letters, digits and hyphens, joined by dots"
        )
    return value


def parse_generated_at(value: str) -> str:
    if _GENERATED_AT_RE.fullmatch(value) is None:
        raise ManifestError(
            "generated_at must be an ISO 8601 UTC instant like 2026-01-31T09:00:00Z"
        )
    return value


def check_signable(payload: dict[str, Any]) -> bytes:
    """The canonical bytes of *payload*, refusing a document over the runtime's cap.

    Checked before signing as well as after: a release the runtime would refuse
    on size must fail while a human is still watching, not once it is on a CDN.
    """
    canonical = canonical_bytes(payload)
    limit = RUNTIME_LIMITS["max_payload_bytes"]
    if len(canonical) > limit:
        raise ManifestError(
            f"signed payload is {len(canonical)} bytes, over the runtime's {limit} byte "
            "limit; every dashboard would refuse it. Split the release or shorten the "
            "entry text."
        )
    return canonical


def check_document_size(manifest_bytes: bytes) -> None:
    """Refuse a published ``manifest.json`` over the runtime's ceiling.

    The consumer reads a bounded number of bytes and refuses the rest, so a file
    that outgrows its limit is a release nobody can fetch. Checked while a person
    is watching rather than once per client.
    """
    limit = RUNTIME_LIMITS["max_document_bytes"]
    if len(manifest_bytes) > limit:
        raise ManifestError(
            f"manifest.json is {len(manifest_bytes)} bytes, over the runtime's {limit} "
            "byte limit; every dashboard would refuse it"
        )


def check_entry_count(count: int) -> None:
    """Refuse a catalog over the runtime's entry ceiling.

    The consumer refuses an over-long entry list whole rather than reading the
    first N, so an over-long release publishes nothing usable.
    """
    limit = RUNTIME_LIMITS["max_entries"]
    if count > limit:
        raise ManifestError(
            f"catalog holds {count} entries, over the runtime's {limit} entry limit; "
            "every dashboard would refuse it"
        )


def check_media_size(name: str, size: int, *, kind: str) -> None:
    """Refuse an empty media file or one over the runtime's cap for its *kind*.

    *kind* is ``"clip"`` or ``"poster"``: the runtime bounds the two differently,
    and an over-cap file is one every dashboard drops or refuses to transfer.
    """
    if size == 0:
        raise ManifestError(f"{name} is empty")
    limit = RUNTIME_LIMITS[f"max_{kind}_bytes"]
    if size > limit:
        raise ManifestError(
            f"{name} is {size} bytes, over the runtime's {limit} byte {kind} limit; every "
            "dashboard would drop it"
        )


def verify_signature(manifest: dict[str, Any], *, public_key: Path) -> str:
    """Verify *manifest*'s signature against *public_key*. Returns its key id.

    Mirrors the runtime's decision, and raises instead of returning False so a
    human running this before an upload is told which part failed. The runtime's
    fail-safe direction is the opposite one — there, unverifiable means untrusted
    and silent — and that asymmetry is deliberate: this side is a person asking
    "is this publishable", that side is a program asking "may I honour this".
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    if manifest.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported manifest schema: {manifest.get('schema')!r}")

    signature_b64 = manifest.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        raise ManifestError("manifest is missing its signature")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ManifestError("manifest signature is not valid base64") from exc
    if not signature or len(signature) > MAX_SIGNATURE_BYTES:
        raise ManifestError("manifest signature has an invalid size")

    expected_key_id = public_key_id(public_key)
    if "key_id" in manifest:
        claimed = manifest["key_id"]
        if not isinstance(claimed, str):
            raise ManifestError("key_id must be a string when present")
        validate_key_id(claimed, where="manifest")
        if claimed != expected_key_id:
            raise ManifestError(
                f"manifest names key_id {claimed} but the verifying key is {expected_key_id}"
            )

    payload = signed_payload(manifest)
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ManifestError(f"manifest is missing field(s): {', '.join(missing)}")
    unknown = set(payload) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise ManifestError(f"manifest carries unknown field(s): {', '.join(sorted(unknown))}")
    canonical = check_signable(payload)

    with tempfile.TemporaryDirectory(prefix="feature-videos-verify-") as scratch:
        root = Path(scratch)
        payload_path = root / "payload.json"
        signature_path = root / "signature.bin"
        payload_path.write_bytes(canonical)
        signature_path.write_bytes(signature)
        try:
            run_openssl(
                [
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_key),
                    "-signature",
                    str(signature_path),
                    str(payload_path),
                ]
            )
        except ManifestError as exc:
            raise ManifestError(
                "signature does not verify against the release key "
                f"({expected_key_id}): tampered bytes or the wrong key"
            ) from exc
    return expected_key_id


def read_bounded(path: Path, *, limit: int, require_owner_only: bool = False) -> bytes:
    """Read at most *limit* bytes from *path*, refusing anything longer.

    Reads ``limit + 1`` and refuses on the extra byte. Reading the whole file and
    measuring it afterwards makes the limit decorative: a multi-gigabyte input
    exhausts memory before the check it was supposed to fail.

    Everything is judged on the one descriptor the bytes come from: it must be a
    regular file, and its size and modification time must be the same after the
    read as before, so the bytes returned are the bytes one whole file held. With
    *require_owner_only* the file must also be one nobody but root or the caller
    can write, and a symlink is refused at open time: the catalog decides every
    title, description, doc link and duration that gets the release signature, so
    a catalog another account could rewrite -- or point elsewhere -- would let
    that account choose signed metadata.
    """
    flags = os.O_RDONLY | _O_NONBLOCK
    if require_owner_only:
        flags |= _O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ManifestError(f"{path} is a symlink; it must be a regular file") from exc
        if exc.errno == errno.ENOENT:
            raise ManifestError(f"missing file: {path}") from exc
        raise ManifestError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError(f"missing file: {path} is not a regular file")
        if require_owner_only and not owner_only(info):
            raise ManifestError(f"{path}: {_OWNER_ONLY_HINT}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
        raise ManifestError(f"{path} changed while it was being read")
    if len(raw) > limit:
        raise ManifestError(f"{path} is larger than {limit} bytes")
    return raw


def load_json_object(path: Path, *, limit: int, require_owner_only: bool = False) -> dict[str, Any]:
    """Read a JSON object from *path*, rejecting duplicate keys and oversize input.

    *require_owner_only* is :func:`read_bounded`'s: the publisher sets it for the
    catalog it signs; the verifier, which trusts nothing it reads, does not.
    """
    raw = read_bounded(path, limit=limit, require_owner_only=require_owner_only)

    def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ManifestError(f"{path}: duplicate JSON key {key!r}")
            seen[key] = value
        return seen

    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        raise ManifestError(f"{path} is nested too deeply to be a catalog") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{path} must hold a JSON object")
    return value
