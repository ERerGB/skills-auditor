"""Private workspace release discovery and verification. Network reads never activate skills."""
import gzip
import hashlib
import io
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .._version import __version__
from ..lifecycle.common import canonical_json
from .common import MAX_BYTES, atomic_json, checked, decode_json, fail, fields, identifier, read_json
from .sources import unpack

PART = 32768
PROFILE = Path.home() / ".config/skills-auditor/catalog.json"
DIGEST = re.compile(r"[0-9a-f]{64}")
OUTCOMES = {"available", "upstream_only", "project_override", "generated_adapter", "held"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def origin(value):
    u = urlsplit(value)
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", value) or not u.hostname:
        fail("catalog_origin", "Enrollment requires a concrete HTTPS origin.")
    return value


def enroll(path, endpoint, workspace):
    profile = {"origin": origin(endpoint), "workspace": identifier(workspace), "token": secrets.token_hex(32)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(canonical_json(profile))
        f.flush()
        os.fsync(f.fileno())
    return {"workspace": workspace, "token_sha256": sha(profile["token"].encode()), "status": "pending_binding"}


def profile_at(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
            fail("catalog_profile", "Use an owner-only regular client profile.")
        profile = fields(decode_json(f.read()), {"origin", "workspace", "token"})
    origin(profile["origin"])
    identifier(profile["workspace"])
    if not DIGEST.fullmatch(profile["token"]):
        fail("catalog_profile", "Invalid client credential format.")
    return profile


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(profile, route, limit=8192):
    request = Request(profile["origin"] + "/v1/" + route,
                      headers={"Authorization": "Bearer " + profile["token"], "User-Agent": "skills-auditor/" + __version__})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = response.read(limit + 1)
    except HTTPError as error:
        fail("catalog_http", "Workspace request denied or unavailable.", http_status=error.code)
    except (URLError, TimeoutError, OSError):
        fail("catalog_transport", "Could not reach the enrolled workspace service.")
    if len(data) > limit:
        fail("catalog_capacity", "Workspace response exceeds its bound.")
    return data


def discover(profile):
    me = decode_json(fetch(profile, "me"))
    if me != {"workspace": profile["workspace"]}:
        fail("catalog_identity", "Server binding differs from enrolled workspace.")
    return me


def lint(bundle):
    fields(bundle, {"schema", "workspace", "items", "attestation", "release"})
    if bundle["schema"] != "skills-auditor-catalog/v1":
        fail("catalog_schema", "Unsupported catalog.")
    identifier(bundle["workspace"])
    att = fields(bundle["attestation"], {"review_sha256", "names", "entry_validator"})
    if not DIGEST.fullmatch(att["review_sha256"]) or not isinstance(att["entry_validator"], str) or not att["entry_validator"]:
        fail("catalog_lint", "Missing lint attestation.")
    items = bundle["items"]
    if not isinstance(items, dict) or not items or type(att["names"]) is not int or att["names"] != len(items):
        fail("catalog_lint", "Inventory and lint count differ.")
    available = {}
    for name, record in items.items():
        if len(name) > 240 or not re.fullmatch(r"[\w.:-]+(?:/[\w.:-]+)*", name, re.ASCII):
            fail("catalog_lint", "Invalid native name.")
        fields(record, {"disposition"}, {"source", "findings"})
        findings = record.get("findings", [])
        if not isinstance(findings, list) or len(findings) > 32 or any(
                not isinstance(f, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", f) for f in findings):
            fail("catalog_lint", "Expected portable lint finding codes.")
        disposition = record["disposition"]
        if disposition not in OUTCOMES or (disposition == "available") != ("source" in record):
            fail("catalog_lint", "Invalid disposition or source mapping.")
        if disposition == "available":
            available[name] = identifier(record["source"])
    release = checked(bundle["release"], "release_id", "release")
    fields(release, {"schema_version", "sources", "release_id"})
    entries = {}
    for source, packet in release["sources"].items():
        identifier(source)
        provenance = fields(packet["origin"], {"kind", "commit", "repository"})
        if (provenance["kind"] != "git" or not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", provenance["repository"])
                or "inspection" not in packet):
            fail("catalog_lint", "Expected an inspected, canonical GitHub package.")
        with tempfile.TemporaryDirectory() as d:
            unpack(packet, Path(d) / "tree")
        for name in packet["entries"]:
            if name in entries:
                fail("catalog_lint", "Release contains duplicate skill identities.")
            entries[name] = source
    if entries != available:
        fail("catalog_lint", "Reviewed entries and installable sources differ.")
    return {"workspace": bundle["workspace"], "names": len(items), "available": len(entries),
            "release_id": release["release_id"], "review_sha256": att["review_sha256"]}


def encode(bundle):
    lint(bundle)
    raw = canonical_json(bundle).encode()
    if len(raw) > MAX_BYTES:
        fail("catalog_capacity", "Release exceeds its size limit.")
    # Fixed gzip header across supported CPython/OS builds.
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as f:
        f.write(raw)
    wire = stream.getvalue()
    if len(wire) > MAX_BYTES:
        fail("catalog_capacity", "Compressed release exceeds its size limit.")
    return wire


def resolve(profile, pin=None):
    workspace = discover(profile)["workspace"]
    if pin is None:
        pointer = fields(decode_json(fetch(profile, workspace + "/current")), {"generation", "digest"})
        if type(pointer["generation"]) is not int or pointer["generation"] < 1:
            fail("catalog_pointer", "Invalid current generation.")
        pin = pointer["digest"]
    if not isinstance(pin, str) or not DIGEST.fullmatch(pin):
        fail("catalog_digest", "Expected a SHA-256 release pin.")
    route = workspace + "/releases/" + pin
    meta = fields(decode_json(fetch(profile, route)), {"size", "parts"})
    size, parts = meta["size"], meta["parts"]
    if type(size) is not int or type(parts) is not int or not 0 < size <= MAX_BYTES or parts != (size + PART - 1) // PART:
        fail("catalog_capacity", "Invalid release size.")
    stream = io.BytesIO()
    for i in range(parts):
        expected = min(PART, size - i * PART)
        piece = fetch(profile, route + "/" + str(i), expected)
        if len(piece) != expected:
            fail("catalog_digest", "Incomplete release.")
        stream.write(piece)
    if sha(stream.getbuffer()) != pin:
        fail("catalog_digest", "Release content differs from pin.")
    stream.seek(0)
    with gzip.GzipFile(fileobj=stream) as compressed:
        raw = compressed.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        fail("catalog_capacity", "Inflated release exceeds its size limit.")
    bundle = decode_json(raw)
    if bundle["workspace"] != workspace:
        fail("catalog_identity", "Release belongs to a different workspace.")
    return bundle, {"digest": pin, **lint(bundle)}


def add_commands(sub):
    root = sub.add_parser("catalog", help="Discover and verify an authenticated workspace release.")
    root.add_argument("--profile", type=Path, default=PROFILE)
    cmds = root.add_subparsers(dest="catalog_command", required=True)
    new = cmds.add_parser("enroll", help="Generate this client's credential and show its binding digest.")
    new.add_argument("--origin", required=True)
    new.add_argument("--workspace", required=True)
    cmds.add_parser("discover")
    local = cmds.add_parser("lint")
    local.add_argument("bundle", type=Path)
    get = cmds.add_parser("resolve")
    get.add_argument("--pin")
    get.add_argument("--out", type=Path, help="Save the verified catalog and portable package privately.")
    plan = cmds.add_parser("plan")
    plan.add_argument("workspaces", nargs="+")
    plan.add_argument("--pin", required=True)
    plan.add_argument("--plan-out", type=Path, required=True)


def run(args, service):
    op = args.catalog_command
    if op == "enroll":
        return enroll(args.profile, args.origin, args.workspace)
    if op == "lint":
        return lint(read_json(args.bundle))
    profile = profile_at(args.profile)
    if op == "discover":
        return discover(profile)
    bundle, receipt = resolve(profile, args.pin)
    if op == "plan":
        return service.plan(args.workspaces, catalog_release=bundle["release"], catalog_digest=receipt["digest"])
    if args.out:
        atomic_json(args.out, bundle)
    return {**receipt, "items": bundle["items"]}
