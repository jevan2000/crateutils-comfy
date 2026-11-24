"""File operations for Copy / Move Files (no ComfyUI imports)."""

from __future__ import annotations

import os

IF_EXISTS = ["add a number", "overwrite", "skip"]


def free_name(dest: "os.PathLike") -> str:
    """dest, or dest with _1, _2... before the extension if it is taken."""
    from pathlib import Path

    d = Path(dest)
    if not d.exists():
        return str(d)
    for n in range(1, 100_000):
        c = d.with_name(f"{d.stem}_{n}{d.suffix}")
        if not c.exists():
            return str(c)
    raise ValueError(f"no free name for {d}")


def copy_or_move(sources: list, dest_dir, mode: str, if_exists: str) -> tuple[list[str], list[str]]:
    """Copy or move files into dest_dir. Returns (new paths, report lines). No ComfyUI imports."""
    import shutil
    from pathlib import Path

    if mode not in ("copy", "move"):
        raise ValueError(f"mode must be 'copy' or 'move', not {mode!r}")
    if if_exists not in IF_EXISTS:
        raise ValueError(f"if_exists must be one of {', '.join(IF_EXISTS)}, not {if_exists!r}")
    new, lines = [], []
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    for src in sources:
        src = Path(src)
        if not src.is_file():
            lines.append(f"{src.name}: not found, skipped")
            continue
        target = Path(dest_dir) / src.name
        if target.resolve() == src.resolve():
            lines.append(f"{src.name}: already there")
            new.append(str(target))
            continue
        if target.exists():
            if if_exists == "skip":
                lines.append(f"{src.name}: exists, skipped")
                continue
            if if_exists == "add a number":
                target = Path(free_name(target))
        if mode == "move":
            shutil.move(str(src), str(target))
        else:
            shutil.copy2(str(src), str(target))
        new.append(str(target))
        lines.append(f"{'moved' if mode == 'move' else 'copied'} {src.name} -> {target}")
    return new, lines
