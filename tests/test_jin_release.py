import json
from pathlib import Path

import pytest

from scripts import jin_release as release


def test_export_verify_load_complete_image_with_immutable_identity(tmp_path, monkeypatch):
    image_id = "sha256:" + "a" * 64
    revision = "b" * 40
    info = {"Id": image_id, "Os": "linux", "Architecture": "amd64", "Config": {"Labels": {
        "com.jin.runtime.core-included": "true", "org.opencontainers.image.revision": revision,
    }}}
    commands = []

    def docker(*args):
        commands.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            return json.dumps([info])
        if args[:3] == ("docker", "image", "save"):
            Path(args[args.index("--output") + 1]).write_bytes(b"docker archive")
        if args[:2] == ("docker", "version"):
            return json.dumps({"Os": "linux", "Arch": "amd64"})
        return ""

    monkeypatch.setattr(release, "run", docker)
    destination = release.export_image("candidate", tmp_path / "release")
    assert release.verify_release(destination)["image_id"] == image_id
    (destination / "image.env").write_text("JIN_RUNTIME_IMAGE=wrong")
    assert release.load_release(destination)["git_revision"] == revision
    assert f"JIN_RUNTIME_IMAGE={image_id}" in (destination / "image.env").read_text()
    assert any("--network" in args and "none" in args for args in commands)
    (destination / "runtime-image.tar").write_bytes(b"damaged")
    with pytest.raises(ValueError, match="checksum/size"):
        release.load_release(destination)


@pytest.mark.parametrize("labels", [{}, {"com.jin.runtime.core-included": "true"}])
def test_incomplete_or_unversioned_images_are_rejected(tmp_path, monkeypatch, labels):
    monkeypatch.setattr(release, "inspect_image", lambda image: {"Config": {"Labels": labels}})
    with pytest.raises(ValueError):
        release.export_image("bad", tmp_path / "release")
    assert not (tmp_path / "release").exists()


def test_release_manifest_cannot_select_an_arbitrary_file(tmp_path):
    (tmp_path / "release.json").write_text(json.dumps({"schema": release.SCHEMA, "archive": "../private"}))
    with pytest.raises(ValueError, match="unsafe"):
        release.verify_release(tmp_path)
