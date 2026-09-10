# feature-videos

Sign a release folder of feature-intro clips for the CDN, and check one before upload.

| File | Does |
|------|------|
| `publish.py` | Reads the folder you assembled, hashes every clip and poster where it is, signs the result, and writes `manifest.json` into that folder. |
| `verify.py` | Re-checks a signed folder: every hash against the bytes on disk, and the signature against the committed release key. |
| `_manifest.py` | The schema, the validation rules, and the runtime's limits. The signing plumbing is not here: it is loaded by path from `packaging/signing/cli-manifest.py`, the CLI release signer. |

Neither script copies, uploads, or reaches the network. `publish.py` prints the
`aws s3 sync` and CloudFront invalidation commands and stops, so the credentials
that can write to a public origin stay with the human running them.

## Layout

Two things, side by side:

```
catalog.json                      one entry per clip (kept OUTSIDE the folder)
dist/feature-videos/<release>/
    monitor-loops.mp4             <id>.mp4 for every entry
    monitor-loops.jpg             <id>.jpg for every entry
```

The catalog stays outside the release folder on purpose: `aws s3 sync` uploads the
whole folder, and the folder must hold nothing the manifest does not sign.
Publishing refuses a folder with any other file in it.

```json
{
  "entries": [
    {
      "id": "monitor-loops",
      "feature": "monitor-loops",
      "title": "Let one session watch a pull request",
      "description": "One or two plain sentences on what the feature does.",
      "doc": "monitor-loops.md",
      "used_when": ["sel_event_seen:monitor_start"],
      "min_version": "",
      "duration_s": 22.0
    }
  ]
}
```

`duration_s` is required. Nothing here inspects the media, so the catalog is the
only place the clip's length can come from, and the dashboard shows it. The
catalog fields themselves are described in
[feature-videos](../../src/kiro_crew/docs/feature-videos.md).

Clips should be H.264 video with AAC or no audio, the formats a browser `<video>`
plays everywhere the dashboard runs. The tool does not check this; the dashboard
plays what it is given.

## Sign

```bash
python3 scripts/feature-videos/publish.py \
  --catalog catalog.json \
  --cdn-host videos.example.com \
  --kms-key-arn "$RELEASE_SIGNING_KEY_ARN"
```

`--release` defaults to the version in `pyproject.toml`, and `--release-dir` to
`dist/feature-videos/<release>`. After it runs, the folder holds the media plus a
signed `manifest.json`, and nothing was moved. The manifest is the whole record:
every digest and size is inside the signed document.

A release folder is immutable. If the folder already holds a `manifest.json`,
publishing refuses: that folder is a release someone may be serving.
Changing a clip means cutting a new release, not re-signing this one.

Signing reuses the CLI artifact manifest's trust root: the same offline key,
`RSASSA_PKCS1_V1_5_SHA_256`, and the same canonical JSON. One release carries one
trust root rather than two. Access to that key is one grant: a principal allowed
to sign feature videos can sign a CLI update manifest with the same key, so
video-signing access is the identical grant as CLI-release signing, never a
looser one.

Keys are separated by purpose. `--kms-key-arn` is the production path — the
private half is a non-exportable AWS KMS key held by the release workflow, so it
exists on no disk — and the tool checks that key's public half against the
committed one before it signs. The manifest then records `key_id` as a hint about
which pinned key was used. `--signing-key <path>` signs with a local key for
staging and tests, omits `key_id`, and warns that the folder is not a release.
The dashboard verifies against the pinned key either way, so a staging folder
stays a staging folder wherever it is uploaded.

`signature` is base64 at the manifest's top level and covers canonical JSON of
every other top-level field, nested values included. Editing one byte of the
manifest breaks it. The canonical rule, the key-id derivation, the openssl and
AWS CLI runners and the pinned KMS flow are the CLI release signer's own code,
`packaging/signing/cli-manifest.py`, loaded by path (its name has a hyphen, so it
cannot be imported). Nothing signing-related is restated in this folder, and
`test/test_feature_videos_publish.py` checks the tool signs with those very
objects and that the runtime's own verifier accepts the result.

What publishing refuses, each with its reason on stderr:

| Refused | Why |
|---------|-----|
| An `id` that is not a lowercase hyphenated slug | The id becomes the asset basename and the display-state key. |
| A missing `<id>.mp4` or `<id>.jpg` | A release folder with a hole in it is not publishable. |
| A file the catalog does not name | It would be uploaded and served under a signature that never covered it. |
| A release folder named through a link at its own path | The tool writes into whatever it is handed; a link there would put a signed manifest somewhere you never named. A link higher up the path is followed once and the folder is then known, signed and printed by its real path, so the `aws s3 sync` line you paste later names no link that could be re-pointed first. |
| A folder already holding `manifest.json` | A release is never re-signed. If an earlier run was interrupted and nothing was uploaded, delete that file and run again; the media is never touched. |
| A clip or poster that changes while it is being read | The digest would cover more bytes than the declared size, and no verifier would accept the release. Finish encoding or copying before signing. |
| A folder, its parent, a clip or poster, or the catalog itself, that another user can write or owns; or a catalog that is a symlink | What is signed is whatever is on disk when it is read. If another account can swap the folder, swap an entry, rewrite a clip or rewrite the catalog, that account decides what gets the release signature -- the catalog alone decides every title, description, doc link and duration. Only you (or root) may be able to change the folder, its media and the catalog. |
| Running `publish.py` on Windows | Windows carries no owner or mode bits this rule can read, so the tool cannot tell who could rewrite the media; rather than sign blind it refuses. Publish from macOS or Linux. `verify.py` works everywhere. |
| A `doc` outside `src/kiro_crew/tips_allowlist.py` | The allowlist tips use, so a clip cannot point at an internal design note. |
| A `--cdn-host` that is not a DNS name or IP address, with nothing but an optional port | The host goes into every signed URL as `https://<host>/feature-videos/<release>/<file>`, and the upload plan puts the folder at the bucket's `feature-videos/<release>/`. A path in the host would be signed into the URLs and served from nowhere; a space or a stray character in it would fail every download with `InvalidURL`. |
| An `openssl` or `aws` whose real file or directory another user can write or owns, or found through a PATH directory anyone can write or another user owns | The tool is handed the signing key path or the KMS authority. Whoever can write any of those three places decides what runs with it. `/usr/bin` and the other root-owned system directories are trusted by name; a Homebrew prefix you own passes, Intel's group-writable `/usr/local/bin` included; the refusal names the file or directory and its mode. |
| A clip or poster that is a symlink, a pipe or anything but a regular file | The bytes that get hashed must be the bytes on disk. |
| An empty file, or a clip or poster over the dashboard's limit for it | The dashboard drops an entry whose clip is over its cap and refuses a poster transfer over its own; the refusal here names the limit. |
| A release that is not `major.minor.patch`, or an `id` over 92 characters | A dashboard only ever asks for a three-component folder, so any other shape is signed and never fetched; and it drops an entry whose `<id>.mp4` is over its 96-character basename bound. |
| A `duration_s` over the dashboard's ceiling (one hour) | The dashboard reads a longer one as unknown and shows nothing. |
| A missing `duration_s`, or one that is not a finite number | The dashboard shows the length; nothing else supplies it. |

## Verify

```bash
python3 scripts/feature-videos/verify.py dist/feature-videos/0.7.0
```

This recomputes every hash from the bytes on disk, verifies the signature against
the committed release key, and refuses a folder carrying a file nobody signed. It
only reads. Run it before every upload.

Pass `--public-key <pem>` to check a folder signed with a staging key.

## Upload

Publishing prints the commands and stops. Run them yourself:

```bash
aws s3 sync --dryrun dist/feature-videos/0.7.0/ s3://BUCKET/feature-videos/0.7.0/
aws s3 sync dist/feature-videos/0.7.0/ s3://BUCKET/feature-videos/0.7.0/
aws cloudfront create-invalidation --distribution-id DISTRIBUTION \
  --paths '/feature-videos/0.7.0/*'
```

The tool enforces immutability on the folder it signs; the bucket side is yours.
`aws s3 sync` overwrites objects freely, so put the guard where the bytes live: a
bucket policy that denies `s3:PutObject` on an existing key, or S3 Object Lock on
the `feature-videos/` prefix. The invalidation is for `manifest.json`, the one
file a dashboard re-reads.

## Size limits

The limits are the dashboard's, and only the dashboard's. A release over any of
them is refused whole (signed payload, `manifest.json` as fetched, entry count) or
has its media dropped (one clip, one poster) by every dashboard, so publishing
refuses it first, exits non-zero, writes nothing, and names the limit it hit.
There is no flag to loosen one: a looser publisher would only move the failure to
where nobody is watching.

The numbers live in the runtime -- `_SIGNED_PAYLOAD_MAX_BYTES`,
`_MANIFEST_MAX_BYTES`, `_MAX_ENTRIES` and `_MAX_ENTRY_BYTES` in
`src/kiro_crew/feature_videos_manifest.py`, `MAX_POSTER_BYTES` in
`src/kiro_crew/feature_videos_cache.py` -- and `_manifest.py` carries them as
`RUNTIME_LIMITS`, which a test holds equal to the source. `verify.py` applies the
same limits to a folder it checks.
