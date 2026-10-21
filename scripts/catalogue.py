"""Publish map releases and build a portable, static catalogue. Python 3.12+."""

import argparse
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile


MANIFEST = "left2mine-map.json"
NUMBER = r"(?:0|[1-9][0-9]{0,8})"
VERSION = rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
TAG = re.compile(rf"([a-z0-9]+(?:-[a-z0-9]+)*)-v({VERSION})")
MAX_ZIP = 2 * 1024**3 - 1
MAX_IMAGE = 1024**2
MAX_JSON = 128 * 1024
TEXT_FIELDS = ("name", "author", "summary", "mode")
# Used for new maps. Existing releases keep their recorded compatibility.
MINECRAFT_VERSION = "1.21.1"
LEFT2MINE_MIN_VERSION = "2.0.0"


def fail(message):
    raise ValueError(message)


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def tag_parts(tag):
    match = TAG.fullmatch(tag)
    if not match or len(tag) > 120:
        fail("Use a release tag such as the-caves-v1.0.0 (lowercase map ID and major.minor.patch).")
    return match.groups()


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def release_identity(inputs, records):
    name = inputs.get("name", "").strip()
    version = inputs.get("version", "").strip()
    if not name or not re.fullmatch(VERSION, version):
        fail("Enter the map name and a map version such as 1.0.0, or leave all fields blank to rebuild.")
    # Preserve IDs assigned by earlier publisher versions when the name matches.
    matching_ids = {m["id"] for _, m in records if m["name"].casefold() == name.casefold()}
    if len(matching_ids) > 1:
        fail("More than one map has this name; give each map a unique name before updating.")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    map_id = next(iter(matching_ids), re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-"))
    if not map_id or len(map_id) > 100:
        fail("Use a shorter map name containing letters A-Z or numbers so a release tag can be generated.")
    tag = f"{map_id}-v{version}"
    tag_parts(tag)
    return tag, map_id, version


def report(message):
    print(message)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write(message + "\n\n")


class GitHub:
    def __init__(self):
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            fail("GITHUB_REPOSITORY must be owner/repository.")
        self.repository = repository
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        origin = urllib.parse.urlsplit(self.base)
        if origin.scheme != "https" and not (
            origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1")
        ):
            fail("The GitHub API must use HTTPS.")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.root = f"/repos/{repository}"
        self.cache = {}

    def request(self, method, path, data=None, content_type="application/json", binary=False):
        url = path if path.startswith(("https://", "http://")) else self.base + path
        target = urllib.parse.urlsplit(url)
        api = urllib.parse.urlsplit(self.base)
        allowed = {(api.scheme, api.netloc), ("https", "uploads.github.com")}
        if (target.scheme, target.netloc) not in allowed:
            fail("Refusing to send credentials to an unexpected upload host.")
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
            "User-Agent": "Left2Mine-Maps-Publisher",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": content_type,
        })
        # Asset downloads redirect to storage. Never forward our token to that host.
        if self.token:
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            return urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"GitHub returned HTTP {error.code} for {method} {target.path}.") from None

    def json(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        with self.request(method, path, body) as response:
            return json.load(response)

    def releases(self):
        releases = []
        page = 1
        while True:
            batch = self.json("GET", f"{self.root}/releases?per_page=100&page={page}")
            if not batch:
                return releases
            releases.extend(batch)
            page += 1

    def download(self, asset, destination, limit):
        if asset.get("state") != "uploaded":
            fail(f"Asset {asset['name']} has not finished uploading.")
        size = asset.get("size", 0)
        if not 0 < size <= limit:
            fail(f"Asset {asset['name']} exceeds its size limit or is empty.")
        digest = hashlib.sha256()
        count = 0
        with self.request("GET", f"{self.root}/releases/assets/{asset['id']}", binary=True) as response:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > limit or count > size:
                    fail(f"Asset {asset['name']} is larger than advertised.")
                destination.write(chunk)
                digest.update(chunk)
        if count != size:
            fail(f"Asset {asset['name']} was not downloaded completely.")
        return digest.hexdigest()

    def small_asset(self, asset, limit=MAX_JSON):
        if asset["id"] not in self.cache:
            destination = io.BytesIO()
            self.download(asset, destination, limit)
            self.cache[asset["id"]] = destination.getvalue()
        content = self.cache[asset["id"]]
        if len(content) > limit:
            fail(f"Asset {asset['name']} exceeds its size limit.")
        return content

    def upload(self, release, name, content, content_type):
        existing = next((a for a in release["assets"] if a["name"] == name), None)
        if existing:
            if self.small_asset(existing, max(MAX_JSON, MAX_IMAGE)) == content:
                return existing
            with self.request("DELETE", f"{self.root}/releases/assets/{existing['id']}"):
                pass
            release["assets"].remove(existing)
        url = release["upload_url"].split("{", 1)[0] + "?" + urllib.parse.urlencode({"name": name})
        with self.request("POST", url, content, content_type) as response:
            asset = json.load(response)
        release["assets"].append(asset)
        self.cache[asset["id"]] = content
        return asset


def asset_named(release, name):
    return next((asset for asset in release["assets"] if asset["name"] == name), None)


def checked_asset(release, record, extension):
    asset = next((a for a in release["assets"] if a["id"] == record.get("asset_id")), None)
    if (
        not asset or asset["size"] != record.get("size")
        or asset.get("state") != "uploaded"
        or not asset["name"].lower().endswith(extension)
        or not re.fullmatch(r"[a-f0-9]{64}", record.get("sha256", ""))
    ):
        fail(f"Release {release['tag_name']} has a missing or changed {extension} asset; publish a new version.")
    return asset


def validate_fields(metadata):
    for field in ("name", "author", "summary", "mode", "left2mine_min_version"):
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            fail(f"Provide {field.replace('_', ' ')} for a new map.")
        if len(value) > (2000 if field == "summary" else 160):
            fail(f"The {field} value is too long.")
    if metadata["mode"] not in ("campaign", "survival"):
        fail("Choose campaign or survival for a new map.")
    if not re.fullmatch(VERSION, metadata["left2mine_min_version"]):
        fail("Use a Left 2 Mine minimum version such as 2.0.0.")
    versions = metadata.get("minecraft_versions")
    if (
        not isinstance(versions, list) or not versions or len(versions) > 20
        or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", v) for v in versions)
    ):
        fail("Provide compatible Minecraft versions, separated by commas (for example 1.21.1).")


def read_manifest(api, release, check_assets=True):
    asset = asset_named(release, MANIFEST)
    if not asset:
        return None
    metadata = json.loads(api.small_asset(asset))
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        fail(f"Unsupported map metadata in {release['tag_name']}.")
    map_id, version = tag_parts(release["tag_name"])
    if metadata.get("id") != map_id or metadata.get("version") != version:
        fail(f"Map metadata does not match release {release['tag_name']}.")
    validate_fields(metadata)
    if check_assets:
        checked_asset(release, metadata["archive"], ".zip")
        if metadata.get("icon"):
            checked_asset(release, metadata["icon"], ".png")
    return metadata


def published_maps(api, releases):
    records = []
    for release in releases:
        if release["draft"] or release["prerelease"] or not TAG.fullmatch(release["tag_name"]):
            continue
        metadata = read_manifest(api, release)
        if metadata:
            records.append((release, metadata))
    return records


def check_icon(content):
    if (
        len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n"
        or content[12:16] != b"IHDR"
    ):
        fail("The world's icon.png must be a PNG image.")
    width, height = struct.unpack(">II", content[16:24])
    if width != 64 or height != 64:
        fail("The world's icon.png must be 64 by 64 pixels, as in Minecraft.")


def world_icon(path, world_path):
    with zipfile.ZipFile(path) as archive:
        try:
            entry = archive.getinfo(world_path + "icon.png")
        except KeyError:
            return None
        if entry.is_dir() or entry.file_size > MAX_IMAGE:
            fail("The world's icon.png must be a file no larger than 1 MiB.")
        image = archive.read(entry)
        check_icon(image)
        return image


def check_world(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 100000 or sum(e.file_size for e in entries) > 8 * 1024**3:
            fail("The map exceeds the expanded ZIP limit (8 GiB / 100,000 entries).")
        names = set()
        worlds = []
        for entry in entries:
            name = entry.filename
            parts = name.rstrip("/").split("/")
            if (
                not name or name.startswith("/") or "\\" in name or ":" in name
                or any(part in ("", ".", "..") for part in parts)
                or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
            ):
                fail(f"Unsafe or encrypted ZIP entry: {name!r}.")
            folded = name.rstrip("/").casefold()
            if folded in names:
                fail(f"Duplicate ZIP entry: {name!r}.")
            names.add(folded)
            if not entry.is_dir() and parts[-1] == "level.dat" and parts[0] != "__MACOSX":
                worlds.append(str(PurePosixPath(name).parent))
        if len(worlds) != 1:
            fail("The ZIP must contain exactly one Minecraft world (one level.dat file).")
        corrupt = archive.testzip()
        if corrupt:
            fail(f"The ZIP contains a damaged file: {corrupt}.")
        return "" if worlds[0] == "." else worlds[0] + "/"


def apply_inputs(metadata, inputs):
    result = dict(metadata)
    for field in TEXT_FIELDS:
        value = inputs.get(field, "").strip()
        if value and not (field == "mode" and value == "keep"):
            result[field] = value
    return result


def publish(api, inputs, records, releases):
    tag, map_id, version = release_identity(inputs, records)
    release = next((r for r in releases if r["tag_name"] == tag), None)
    # Different names can reduce to the same tag. Never silently merge those maps.
    known = [m for _, m in records if m["id"] == map_id]
    for draft in releases:
        if draft["draft"] and draft["tag_name"].startswith(map_id + "-v") and TAG.fullmatch(draft["tag_name"]):
            if saved := read_manifest(api, draft, check_assets=False):
                known.append(saved)
    if any(m["name"].casefold() != inputs["name"].strip().casefold() for m in known):
        fail("That name generates the tag of a different map. Use a distinct name.")
    if release and release["prerelease"]:
        fail("Turn off the prerelease checkbox before publishing a catalogue map.")
    if release and not release["draft"]:
        existing = read_manifest(api, release)
        if existing is None:
            fail("This release is already public but has no map metadata. Prepare a draft release instead.")
        if apply_inputs(existing, inputs) != existing:
            fail("Published details are fixed. Create a new version to change them.")
        report(f"Release `{tag}` is already published; rebuilding the catalogue.")
        return release, existing

    earlier = sorted(
        ((r, m) for r, m in records if m["id"] == map_id),
        key=lambda record: version_key(record[1]["version"]), reverse=True,
    )
    # A failed run may already have attached metadata. Reuse it when retrying.
    saved = read_manifest(api, release, check_assets=False) if release else None
    base = saved or (earlier[0][1] if earlier else {})
    metadata = apply_inputs(base, inputs)
    metadata.update({"schema_version": 1, "id": map_id, "version": version})
    metadata.setdefault("minecraft_versions", [MINECRAFT_VERSION])
    metadata.setdefault("left2mine_min_version", LEFT2MINE_MIN_VERSION)
    metadata.pop("archive", None)
    metadata.pop("icon", None)
    validate_fields(metadata)

    if release is None:
        release = api.json("POST", f"{api.root}/releases", {
            "tag_name": tag, "name": f"{metadata['name']} {version}",
            "draft": True, "prerelease": False, "make_latest": "false",
        })
    zips = [a for a in release["assets"] if a["name"].lower().endswith(".zip")]
    if not zips:
        api.upload(release, MANIFEST, (json.dumps(metadata, indent=2) + "\n").encode(), "application/json")
        report(
            f"Draft `{tag}` is ready. [Open the draft release]({release['html_url']}).\n\n"
            "Edit it, attach one world ZIP, and choose **Save draft**. Then rerun this workflow "
            "with the same name and map version (or use **Re-run all jobs** on this run). "
            "The remaining details have been saved. This map is not published yet."
        )
        return None
    if len(zips) != 1:
        fail("Attach exactly one map ZIP to the draft release.")

    with tempfile.TemporaryDirectory(prefix="left2mine-publish-") as directory:
        path = Path(directory) / "world.zip"
        with path.open("wb") as destination:
            digest = api.download(zips[0], destination, MAX_ZIP)
        world_path = check_world(path)
        image = world_icon(path, world_path)

    # All input and archive checks finish before altering or publishing the draft.
    if image is not None:
        image_asset = api.upload(release, "icon.png", image, "image/png")
        metadata["icon"] = {
            "asset_id": image_asset["id"], "size": image_asset["size"], "sha256": sha256(image),
        }
    metadata["archive"] = {
        "asset_id": zips[0]["id"], "size": zips[0]["size"],
        "sha256": digest, "world_path": world_path,
    }
    api.upload(release, MANIFEST, (json.dumps(metadata, indent=2) + "\n").encode(), "application/json")
    release = api.json("PATCH", f"{api.root}/releases/{release['id']}", {
        "draft": False, "make_latest": "false",
    })
    report(f"Published [{tag}]({release['html_url']}). The catalogue is ready for Pages deployment.")
    return release, metadata


def build_site(api, records, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "icons").mkdir(exist_ok=True)
    grouped = {}
    records.sort(key=lambda record: version_key(record[1]["version"]), reverse=True)
    for release, metadata in records:
        archive = checked_asset(release, metadata["archive"], ".zip")
        icon_fields = {}
        if metadata.get("icon"):
            image_asset = checked_asset(release, metadata["icon"], ".png")
            image = api.small_asset(image_asset, MAX_IMAGE)
            digest = metadata["icon"]["sha256"]
            if sha256(image) != digest:
                fail(f"World icon checksum failed for {release['tag_name']}.")
            check_icon(image)
            image_path = f"icons/{digest}.png"
            (output / image_path).write_bytes(image)
            icon_fields = {"icon": image_path, "icon_sha256": digest}
        download_url = archive["browser_download_url"]
        if not download_url.startswith(f"https://github.com/{api.repository}/releases/download/"):
            fail("A release has an unexpected public download URL.")
        entry = grouped.setdefault(metadata["id"], {
            field: metadata[field] for field in ("id", "name", "author", "summary", "mode")
        })
        entry.setdefault("versions", []).append({
            "version": metadata["version"],
            "minecraft_versions": metadata["minecraft_versions"],
            "left2mine_min_version": metadata["left2mine_min_version"],
            "download_url": download_url,
            "file_size": archive["size"],
            "sha256": metadata["archive"]["sha256"],
            "world_path": metadata["archive"]["world_path"],
            **icon_fields,
            "release_url": release["html_url"],
            "published_at": release["published_at"],
        })
    catalogue = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "maps": sorted(grouped.values(), key=lambda entry: entry["name"].casefold()),
    }
    # Write the catalogue only after every record has passed validation.
    (output / "catalogue.json").write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    (output / "index.html").write_text(render_index(catalogue), encoding="utf-8")
    print(f"Built {len(catalogue['maps'])} maps into {output}.")


def render_index(catalogue):
    escape = html.escape
    cards = []
    for entry in catalogue["maps"]:
        version = entry["versions"][0]
        image = f'<img src="{escape(version["icon"])}" alt="" loading="lazy">' if "icon" in version else ""
        cards.append(
            f'<article>{image}'
            f'<h2>{escape(entry["name"])}</h2><p>By {escape(entry["author"])}</p>'
            f'<p>{escape(entry["summary"])}</p>'
            f'<p>{escape(entry["mode"].title())} · Version {escape(version["version"])}</p>'
            f'<p>Minecraft {escape(", ".join(version["minecraft_versions"]))} · '
            f'Left 2 Mine {escape(version["left2mine_min_version"])} or newer</p>'
            f'<a href="{escape(version["download_url"])}">Download map ZIP</a></article>'
        )
    body = "\n".join(cards) or "<p>No maps published yet.</p>"
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Left 2 Mine Maps</title><style>'
        'body{font:16px/1.5 system-ui,sans-serif;max-width:1000px;margin:40px auto;'
        'padding:0 20px;background:#171b1e;color:#eee}'
        'a{color:#a7d78d}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}'
        'article{padding:20px;background:#242b30;border-radius:8px}'
        'img{width:64px;height:64px;image-rendering:pixelated}h2{margin-bottom:0}'
        '</style><header><h1>Left 2 Mine Maps</h1>'
        '<p>Community maps for Left 2 Mine. <a href="catalogue.json">Download catalogue</a></p>'
        f'</header><main>{body}</main></html>\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("_site"))
    args = parser.parse_args()
    try:
        if args.output.exists() and any(args.output.iterdir()):
            fail("The output directory must be empty; choose a new folder.")
        inputs = json.loads(os.environ.get("MAP_INPUTS", "{}") or "{}") or {}
        if not isinstance(inputs, dict) or any(not isinstance(v, str) for v in inputs.values()):
            fail("Workflow inputs must be strings.")
        if set(inputs) - set(TEXT_FIELDS) - {"version"}:
            fail("Use only name, author, summary, version and mode in the publishing form.")
        api = GitHub()
        releases = api.releases()
        records = published_maps(api, releases)
        if any(v.strip() and not (k == "mode" and v.strip() == "keep") for k, v in inputs.items()):
            if result := publish(api, inputs, records, releases):
                release, metadata = result
                records = [(r, m) for r, m in records if r["id"] != release["id"]]
                records.append((release, metadata))
        build_site(api, records, args.output)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
