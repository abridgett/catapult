"""
Commands to manage releases.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import dataclasses
import invoke
import pygit2 as git
import pytz

from catapult import utils

LOG = logging.getLogger(__name__)


@dataclasses.dataclass
class Release:

    version: int
    commit: str
    version_id: str
    image: str
    timestamp: datetime
    author: str
    changelog: str
    rollback: bool = False


class InvalidRelease(Exception):
    """
    Raised when the stored release is missing data or has an invalid format.
    """


def _get_release(client, bucket, key, version_id=None) -> Release:
    """
    Fetches a release from a S3 object.

    Arguments:
        client (botocore.client.S3): client for AWS S3.
        bucket (str): bucket's name.
        key (str): object's key.
        version_id (str or None): version ID of the S3 object.
            If the `version_id` is `None`, it will return the latest release.

    Returns:
        Release or None: the release stored in the object.
    """
    extras = {}

    if version_id is not None:
        extras["VersionId"] = version_id

    try:
        resp = client.get_object(Bucket=bucket, Key=key, **extras)

    except client.exceptions.NoSuchKey:
        raise InvalidRelease(f"Key not found: {key}")

    try:
        body = json.load(resp["Body"])

    except json.JSONDecodeError as exc:
        raise InvalidRelease("Invalid JSON data") from exc

    try:
        version = body["version"]
        commit = body["commit"]
        image = body["image"]
        author = body["author"]
        rollback = body.get("rollback", False)

    except KeyError as exc:
        raise InvalidRelease(f"Missing property in JSON: {exc}")

    if "VersionId" not in resp:
        # files created when the bucket had the versioning disabled
        raise InvalidRelease("Object has no S3 VersionId")

    return Release(
        version=version,
        commit=commit,
        changelog=body.get("changelog", "<changelog unavailable>"),
        version_id=resp["VersionId"],
        image=image,
        timestamp=resp["LastModified"],
        author=author,
        rollback=rollback,
    )


def _get_versions(client, bucket, key):
    resp = client.list_object_versions(Bucket=bucket, Prefix=key)

    for version in resp.get("Versions", []):
        if version["Key"] != key:
            continue

        yield version


_DATETIME_MAX = pytz.utc.localize(datetime.max)


def _get_bucket():
    config = utils.get_config()
    if config:
        return config["release"]["s3_bucket"]
    return os.environ["CATAPULT_BUCKET_RELEASES"]


def get_releases(client, key, since=None, bucket=None):
    """
    Gets all the releases in the project's history.

    Arguments:
        client (botocore.client.S3): client for AWS S3.
        bucket (str): bucket's name.
        key (str): object's key.
        since (int or None): exclude version created before this version.

    Yield:
        Release a release in the project's history.
    """
    if bucket is None:
        bucket = _get_bucket()

    versions = sorted(
        _get_versions(client, bucket, key),
        key=lambda v: _DATETIME_MAX if v["IsLatest"] else v["LastModified"],
        reverse=True,
    )

    for version in versions:
        try:
            release = _get_release(client, bucket, key, version["VersionId"])

        except InvalidRelease as exc:
            # skip invalid releases in object history
            LOG.warning(f"Invalid release object: {exc}")
            continue

        if since and release.version < since:
            continue

        yield release


def get_release(client, key, version=None, bucket=None):
    """
    Fetches a specific release.

    Arguments:
        client (botocore.client.S3): client for AWS S3.
        bucket (str): bucket's name.
        key (str): object's key.
        version (int): version number.

    Returns:
        Release or None: the release identified by the given version.
            `None` if the version does not exist.
    """
    if bucket is None:
        bucket = _get_bucket()

    for release in get_releases(client, key, bucket=bucket):
        if release.version == version or version is None:
            return release

    return None


def put_release(client, bucket, key, release):
    """
    Upload a new release to S3.

    Arguments:
        client (botocore.client.S3): client for AWS S3.
        bucket (str): bucket's name.
        key (str): object's key.
        release (Release): release to upload.

    Returns:
        Release: uploaded release with the updated fields.
    """
    resp = client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(
            {
                "version": release.version,
                "commit": release.commit,
                "changelog": release.changelog,
                "image": release.image,
                "author": release.author,
                "rollback": release.rollback,
            }
        ),
    )

    return dataclasses.replace(
        release,
        version_id=resp["VersionId"],
        timestamp=pytz.utc.localize(datetime.utcnow()),
    )


def _get_image_id(ctx, commit: git.Oid, *, name: str, image_name: Optional[str]):
    image_base = utils.get_config()["release"]["docker_repository"]

    if image_name is None:
        image_prefix = utils.get_config()["release"]["docker_image_prefix"]
        image_name = f"{image_prefix}{name}"

    image = f"{image_base}/{image_name}:ref-{commit.hex}"

    LOG.info(f"Pulling {image}")
    res = ctx.run(f"docker pull {image}", hide="out")

    for line in res.stdout.split("\n"):
        if line.startswith("Digest:"):
            _, _, image_id = line.partition(":")
            return image_id.strip()

    return None


@invoke.task(help={"name": "project's name"})
@utils.require_2fa
def current(_, name):
    """
    Show current release.
    """
    release = next(get_releases(utils.s3_client(), name), None)

    if release:
        utils.printfmt(release)

    else:
        utils.fatal("Release does not exist")


@invoke.task(help={"name": "project's name", "version": "release's version"})
@utils.require_2fa
def get(_, name, version):
    """
    Show the release.
    """
    release = get_release(utils.s3_client(), name, int(version))

    if release:
        utils.printfmt(release)

    else:
        utils.fatal("Release does not exist")


@invoke.task(
    help={
        "name": "project's name",
        "last": "return only the last n releases",
        "contains": "commit hash or revision of a commit, eg `bcc31bc`, `HEAD`, `some_branch`",
    }
)
@utils.require_2fa
def ls(_, name, last=None, contains=None):
    """
    Show all the project's releases.
    """
    repo = None
    contains_oid = None

    if contains:
        repo = utils.git_repo()
        contains_oid = utils.revparse(repo, contains)
        if contains_oid not in repo:
            raise Exception(f"Commit {contains_oid} does not exist in repo")

    releases = get_releases(utils.s3_client(), name)

    release_data = []
    now = datetime.now(tz=timezone.utc)
    last = int(last) if last else None
    for i, rel in enumerate(releases):
        if i == last:
            break
        release_dict = {
            "version": rel.version,
            "commit": rel.commit,
            "timestamp": rel.timestamp,
            "age": now - rel.timestamp,
            "author": rel.author,
            "rollback": rel.rollback,
        }
        if contains:
            release_dict["contains"] = release_contains(repo, rel, contains_oid, name)
        release_data.append(release_dict)

    utils.printfmt(release_data, tabular=True)


@invoke.task(
    help={
        "name": "identifies the project to release.",
        "commit": "git ref to build from.",
        "version": "new version",
        "image-id": "ID of the docker image to release.",
        "image-name": "name of the image to release (default to name)",
        "dry": "prepare a release without committing it",
        "yes": "Automatic yes to prompt",
        "rollback": "needed to start a rollback",
    },
    default=True,
)
@utils.require_2fa
def new(
    ctx,
    name,
    commit=None,
    version=None,
    dry=False,
    yes=False,
    image_name=None,
    image_id=None,
    rollback=False,
):
    """
    Create a new release.
    """
    repo = utils.git_repo()

    client = utils.s3_client()
    latest = next(get_releases(client, name), None)
    latest_oid = git.Oid(hex=latest.commit) if latest else None

    if commit is None:
        commit = "HEAD"

    commit_oid = utils.revparse(repo, commit)

    if version is None:
        # create next version
        version = 1 if latest is None else latest.version + 1

    else:
        version = int(version)

    if image_id is None:
        image_id = _get_image_id(ctx, commit_oid, name=name, image_name=image_name)

        if image_id is None:
            utils.fatal("Image not found")

    changelog = utils.changelog(repo, commit_oid, latest_oid)

    release = Release(
        version=version,
        commit=commit_oid.hex,
        changelog=changelog.text,
        version_id="",
        image=image_id,
        timestamp=datetime.now(),
        author=utils.get_author(repo),
        rollback=changelog.rollback,
    )

    utils.printfmt(release)

    if dry:
        return

    if release.rollback:
        utils.warning("This is a rollback! :warning:\n")

        if not rollback:
            utils.warning("Missing flag --rollback\n")
            utils.fatal("Aborted!")

    if not yes:

        if release.rollback:
            ok = utils.confirm(
                "Are you sure you want to create a rollback release?",
                style=utils.TextStyle.yellow,
            )

            if not ok:
                utils.fatal("Aborted!")

        ok = utils.confirm("Are you sure you want to create this release?")
        if not ok:
            utils.fatal("Aborted!")

    put_release(client, _get_bucket(), name, release)

    utils.success("Created new release :tada:\n")


@invoke.task(
    help={
        "name": "identifies the project to release.",
        "commit": "git ref of the release to look for.",
    }
)
@utils.require_2fa
def find(_, name, commit=None):
    """
    Find the first release containing a specific commit.
    """
    if commit is None:
        commit = "HEAD"

    repo = utils.git_repo()
    oid = utils.revparse(repo, commit)

    client = utils.s3_client()

    releases = {release.commit: release for release in get_releases(client, name)}
    print(len(releases))

    release = None
    for log in utils.git_log(repo):
        print(oid.hex, release and release.commit, log.hex)
        if log.hex in releases:
            release = releases[log.hex]

        if oid.hex == log.hex:
            break

    if release:
        utils.printfmt(release)

    else:
        LOG.error("Commit not released yet")


@invoke.task(
    help={
        "git_range": "identifies the project to release.",
        "resolve": "transform the version range into a valid git log range",
    }
)
@utils.require_2fa
def log(_, name, git_range, resolve=False):
    """
    Search a release from the commit hash.
    """
    repo = utils.git_repo()

    client = utils.s3_client()

    lx, _, rx = git_range.partition("..")

    def resolve_range(ref):
        if ref.startswith("v") and ref[1:].isdigit():
            release = get_release(client, name, int(ref[1:]))

            return release.commit

        return ref

    start = resolve_range(lx)
    end = resolve_range(rx)

    if resolve:
        text = f"{start}...{end}"

    else:
        text = utils.changelog(repo, end, start).text

    print(text)


def release_contains(
    repo: git.Repository, release: Release, commit_oid: git.Oid, name: str
):
    if not release.commit:
        LOG.warning(f"{name} has a null commit ref")
        return "?"

    release_oid = git.Oid(hex=release.commit)
    try:
        in_release = utils.commit_contains(repo, release_oid, commit_oid)
    except git.GitError as e:
        LOG.warning(f"Repo: [{repo.workdir}], Error: [{repr(e)}], Project: [{name}]")
        in_release = "?"

    return in_release


release = invoke.Collection("release", current, ls, new, find, get, log)
