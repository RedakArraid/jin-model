"""Resolve dataset-relative files, including pre-portability Studio exports."""
from pathlib import Path, PurePosixPath, PureWindowsPath


def dataset_file(manifest: Path, value: str) -> Path:
    normalized = str(value).replace("\\", "/")
    path = Path(normalized)
    absolute = path.is_absolute() or PureWindowsPath(normalized).is_absolute()
    if absolute:
        parts = PurePosixPath(normalized).parts
        # Relocate legacy Studio PDF/annotation references before trying the
        # obsolete absolute path (which may still exist on the old host).
        if manifest.parent.parent.name == "datasets":
            for namespace, root in (("documents", manifest.parent.parent.parent),
                                    ("annotations", manifest.parent)):
                if namespace in parts:
                    tail = parts[len(parts) - 1 - parts[::-1].index(namespace):]
                    if ".." in tail:
                        raise ValueError("Unsafe legacy dataset path")
                    candidate = (root / Path(*tail)).resolve()
                    if root.resolve() not in candidate.parents:
                        raise ValueError("Dataset path escapes storage")
                    return candidate
        if "images" in parts:
            tail = parts[len(parts) - 1 - parts[::-1].index("images"):]
            if ".." in tail:
                raise ValueError("Unsafe legacy image path")
            return (manifest.parent / Path(*tail)).resolve()
        if PureWindowsPath(normalized).is_absolute() and not path.is_absolute():
            raise ValueError("Unrecognized foreign absolute dataset path")
        return path
    base = manifest.parent.parent if normalized.startswith("corpus/") else manifest.parent
    return (base / path).resolve()


def materialize_page_paths(row: dict, manifest: Path) -> dict:
    result = dict(row)
    if result.get("image_path"):
        result["image_path"] = str(dataset_file(manifest, result["image_path"]))
    return result
