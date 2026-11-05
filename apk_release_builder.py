#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Универсальный UI: флаги/версия из app.dart + релизная сборка APK."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "apk_release_builder_config.json"

VERSION_NAMES = ("major", "minor", "patch", "kAppVersionCode")

# Папка и имя APK, которые создаёт `flutter build apk --release`.
APK_OUTPUT_DIR = Path("build") / "app" / "outputs" / "flutter-apk"
APK_DEFAULT_NAME = "app-release.apk"
# Сколько последних открытых проектов помнить.
RECENT_PROJECTS_LIMIT = 15
# Недопустимые в имени файла Windows символы.
_RE_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\s]+')
_RE_PUBSPEC_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)

# const bool/int/String name = ...;
_RE_TYPED = re.compile(
    r"^const\s+(bool|int|String)\s+(\w+)\s*=\s*(.+?)\s*;\s*$",
    re.MULTILINE,
)
# const Type name = Type.value;  (enum, значение может быть на следующей строке)
_RE_ENUM_CONST = re.compile(
    r"^const\s+(\w+)\s+(\w+)\s*=\s*\1\.(\w+)\s*;\s*$"
    r"|^const\s+(\w+)\s+(\w+)\s*=\s*\n\s*\4\.(\w+)\s*;\s*$",
    re.MULTILINE,
)
# const major = 2;  (untyped int aliases)
_RE_BARE_INT = re.compile(
    r"^const\s+(major|minor|patch)\s*=\s*(-?\d+)\s*;\s*$",
    re.MULTILINE,
)
_RE_VERSION_CODE = re.compile(
    r"^const\s+int\s+kAppVersionCode\s*=\s*(-?\d+)\s*;\s*$",
    re.MULTILINE,
)
_RE_DOC = re.compile(r"^///\s?(.*)$")
_RE_IMPORT = re.compile(
    r"""^import\s+['"]([^'"]+)['"]\s*;""",
    re.MULTILINE,
)
_RE_ENUM_DECL = re.compile(
    r"enum\s+(\w+)\s*\{([^}]*)\}",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class FlagDef:
    name: str
    label: str
    kind: str  # bool | int | string | enum
    value: str | bool | int
    enum_type: str | None = None
    enum_options: list[str] = field(default_factory=list)
    # line span for rewrite (full match of const line)
    match_start: int = 0
    match_end: int = 0


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def default_project_root() -> Path:
    # farbuilder лежит в корне Flutter-проекта.
    return TOOL_DIR.parent


def project_key(root: str | Path) -> str:
    """Ключ проекта в конфиге: нормализованный абсолютный путь."""
    return str(Path(root).resolve())


def migrate_config(cfg: dict) -> dict:
    """Старый формат (один проект на верхнем уровне) → projects[путь] + история."""
    cfg = dict(cfg)
    projects = dict(cfg.get("projects") or {})
    old_root = cfg.pop("project_root", None)
    old = {k: cfg.pop(k) for k in ("app_dart", "pubspec", "flags") if k in cfg}
    for k in ("major", "minor", "patch", "versionCode"):
        cfg.pop(k, None)
    if old_root:
        key = project_key(old_root)
        projects.setdefault(key, old)
        recent = list(cfg.get("recent_projects") or [])
        if key not in recent:
            recent.insert(0, key)
        cfg["recent_projects"] = recent
        cfg.setdefault("last_project", key)
    cfg["projects"] = projects
    return cfg


# Что дописывается к имени APK: версия (1.2.5) или номер сборки (versionCode).
APK_SUFFIX_VERSION = "version"
APK_SUFFIX_BUILD = "build"


def apk_suffix_value(kind: str, major: int, minor: int, patch: int, version_code: int) -> str:
    if kind == APK_SUFFIX_BUILD:
        return str(version_code)
    return f"{major}.{minor}.{patch}"


def apk_file_name(base: str, suffix: str) -> str:
    """Имя как введено + версия или номер сборки, без разделителя:
    test_app_v + 1.2.5 → test_app_v1.2.5.apk."""
    clean = _RE_BAD_FILENAME_CHARS.sub("_", base.strip())
    if not clean:
        raise ValueError("Укажите имя APK")
    return f"{clean}{suffix}.apk"


def pubspec_app_name(pub: Path) -> str:
    try:
        m = _RE_PUBSPEC_NAME.search(read_text(pub))
    except Exception:
        return ""
    return m.group(1) if m else ""


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — открыть в Проводнике
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def resolve_paths(cfg: dict) -> tuple[Path, Path, Path]:
    root = Path(cfg.get("project_root") or default_project_root()).resolve()
    app_rel = cfg.get("app_dart") or "lib/src/app.dart"
    pub_rel = cfg.get("pubspec") or "pubspec.yaml"
    app = (root / app_rel).resolve()
    pub = (root / pub_rel).resolve()
    return root, app, pub


def _doc_label_before(src: str, pos: int) -> str | None:
    """Первая строка ближайшего блока /// непосредственно над pos."""
    before = src[:pos].rstrip("\n")
    if not before:
        return None
    lines = before.split("\n")
    doc_lines: list[str] = []
    i = len(lines) - 1
    while i >= 0:
        raw = lines[i].rstrip()
        if not raw.strip():
            if doc_lines:
                break
            i -= 1
            continue
        m = _RE_DOC.match(raw.strip())
        if m:
            doc_lines.append(m.group(1).strip())
            i -= 1
            continue
        break
    if not doc_lines:
        return None
    doc_lines.reverse()
    return doc_lines[0] or None


def _parse_bool_literal(raw: str) -> bool | None:
    s = raw.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    return None


def _parse_string_literal(raw: str) -> str | None:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return None


def _parse_int_literal(raw: str) -> int | None:
    s = raw.strip()
    try:
        return int(s, 10)
    except ValueError:
        return None


def _collect_imports(src: str) -> list[str]:
    return [m.group(1) for m in _RE_IMPORT.finditer(src)]


def _resolve_import_path(app_dart: Path, import_uri: str) -> Path | None:
    if import_uri.startswith("package:"):
        return None
    # relative to app.dart directory
    return (app_dart.parent / import_uri).resolve()


def _find_enum_options(
    enum_type: str,
    app_dart: Path,
    app_src: str,
) -> list[str]:
    # 1) enum in the same file
    for m in _RE_ENUM_DECL.finditer(app_src):
        if m.group(1) == enum_type:
            return _enum_members(m.group(2))

    # 2) relative imports from app.dart
    for uri in _collect_imports(app_src):
        path = _resolve_import_path(app_dart, uri)
        if path is None or not path.is_file():
            continue
        try:
            text = read_text(path)
        except Exception:
            continue
        for m in _RE_ENUM_DECL.finditer(text):
            if m.group(1) == enum_type:
                return _enum_members(m.group(2))
    return []


def _enum_members(body: str) -> list[str]:
    members: list[str] = []
    for part in body.split(","):
        ident = None
        for line in part.splitlines():
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            m = re.match(r"(\w+)", s)
            if m:
                ident = m.group(1)
                break
        if ident:
            members.append(ident)
    return members


def parse_version(src: str) -> dict[str, int]:
    major = minor = patch = None
    for m in _RE_BARE_INT.finditer(src):
        name, val = m.group(1), int(m.group(2))
        if name == "major":
            major = val
        elif name == "minor":
            minor = val
        elif name == "patch":
            patch = val
    code_m = _RE_VERSION_CODE.search(src)
    if major is None or minor is None or patch is None or not code_m:
        raise ValueError(
            "В app.dart нужны const major/minor/patch и const int kAppVersionCode"
        )
    return {
        "major": major,
        "minor": minor,
        "patch": patch,
        "versionCode": int(code_m.group(1)),
    }


def parse_flags(app_src: str, app_dart: Path) -> list[FlagDef]:
    flags: list[FlagDef] = []
    seen: set[str] = set()

    for m in _RE_TYPED.finditer(app_src):
        typ, name, raw = m.group(1), m.group(2), m.group(3)
        if name in VERSION_NAMES or name == "kAppVersionCode":
            continue
        label = _doc_label_before(app_src, m.start())
        if not label:
            continue
        if typ == "bool":
            val = _parse_bool_literal(raw)
            if val is None:
                continue
            kind, value = "bool", val
        elif typ == "int":
            val = _parse_int_literal(raw)
            if val is None:
                continue
            kind, value = "int", val
        else:  # String
            val = _parse_string_literal(raw)
            if val is None:
                continue
            kind, value = "string", val
        flags.append(
            FlagDef(
                name=name,
                label=label,
                kind=kind,
                value=value,
                match_start=m.start(),
                match_end=m.end(),
            )
        )
        seen.add(name)

    for m in _RE_ENUM_CONST.finditer(app_src):
        enum_type, name, member = m.group(1), m.group(2), m.group(3)
        if name in seen:
            continue
        label = _doc_label_before(app_src, m.start())
        if not label:
            continue
        options = _find_enum_options(enum_type, app_dart, app_src)
        flags.append(
            FlagDef(
                name=name,
                label=label,
                kind="enum",
                value=member,
                enum_type=enum_type,
                enum_options=options,
                match_start=m.start(),
                match_end=m.end(),
            )
        )
        seen.add(name)

    # stable order by appearance
    flags.sort(key=lambda f: f.match_start)
    return flags


def apply_version(src: str, cfg: dict) -> str:
    out = src
    out = re.sub(
        r"(const\s+major\s*=\s*)-?\d+(\s*;)",
        rf"\g<1>{cfg['major']}\2",
        out,
        count=1,
    )
    out = re.sub(
        r"(const\s+minor\s*=\s*)-?\d+(\s*;)",
        rf"\g<1>{cfg['minor']}\2",
        out,
        count=1,
    )
    out = re.sub(
        r"(const\s+patch\s*=\s*)-?\d+(\s*;)",
        rf"\g<1>{cfg['patch']}\2",
        out,
        count=1,
    )
    out = re.sub(
        r"(const\s+int\s+kAppVersionCode\s*=\s*)-?\d+(\s*;)",
        rf"\g<1>{cfg['versionCode']}\2",
        out,
        count=1,
    )
    return out


def apply_flag_values(src: str, flags: list[FlagDef], values: dict) -> str:
    # Replace from end so offsets stay valid
    ordered = sorted(flags, key=lambda f: f.match_start, reverse=True)
    out = src
    for f in ordered:
        if f.name not in values:
            continue
        new_val = values[f.name]
        chunk = out[f.match_start : f.match_end]
        if f.kind == "bool":
            lit = "true" if new_val else "false"
            chunk2, n = re.subn(
                rf"(const\s+bool\s+{f.name}\s*=\s*)(true|false)(\s*;)",
                rf"\g<1>{lit}\3",
                chunk,
                count=1,
            )
        elif f.kind == "int":
            chunk2, n = re.subn(
                rf"(const\s+int\s+{f.name}\s*=\s*)-?\d+(\s*;)",
                rf"\g<1>{int(new_val)}\2",
                chunk,
                count=1,
            )
        elif f.kind == "string":
            # preserve quote style of original if possible
            q = "'" if "'" in chunk else '"'
            esc = str(new_val).replace("\\", "\\\\").replace(q, f"\\{q}")
            chunk2, n = re.subn(
                rf'(const\s+String\s+{f.name}\s*=\s*)([\'"]).*\2(\s*;)',
                rf"\g<1>{q}{esc}{q}\3",
                chunk,
                count=1,
            )
        elif f.kind == "enum":
            et = f.enum_type or ""
            chunk2, n = re.subn(
                rf"(const\s+{et}\s+{f.name}\s*=\s*{et}\.)\w+(\s*;)",
                rf"\g<1>{new_val}\2",
                chunk,
                count=1,
            )
        else:
            continue
        if n != 1:
            raise ValueError(f"Не удалось записать {f.name}")
        out = out[: f.match_start] + chunk2 + out[f.match_end :]
    return out


def apply_pubspec(src: str, cfg: dict) -> str:
    ver = f"{cfg['major']}.{cfg['minor']}.{cfg['patch']}+{cfg['versionCode']}"
    out, n = re.subn(
        r"^version:\s*.+$",
        f"version: {ver}",
        src,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise ValueError("Не удалось обновить version в pubspec.yaml")
    return out


class BuilderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Feature flags — Release APK")
        self.geometry("720x860")
        self.minsize(640, 700)

        self._cfg_disk = migrate_config(load_config())
        start = self._cfg_disk.get("last_project") or str(default_project_root())
        self.project_root = tk.StringVar(value=project_key(start))
        self.app_dart_rel = tk.StringVar()
        self.pubspec_rel = tk.StringVar()
        # Имя APK как введено + версия или номер сборки: «test_app_v» → test_app_v1.2.5.apk
        self.apk_name_var = tk.StringVar()
        self.apk_suffix_var = tk.StringVar(value=APK_SUFFIX_VERSION)
        self.apk_preview_var = tk.StringVar()
        self._load_project_settings(start)

        self.major_var = tk.IntVar(value=0)
        self.minor_var = tk.IntVar(value=0)
        self.patch_var = tk.IntVar(value=0)
        self.code_var = tk.IntVar(value=1)
        for var in (
            self.major_var,
            self.minor_var,
            self.patch_var,
            self.code_var,
            self.apk_name_var,
            self.apk_suffix_var,
        ):
            var.trace_add("write", lambda *_: self._update_apk_preview())

        self._flags: list[FlagDef] = []
        self._flag_vars: dict[str, tk.Variable] = {}
        self._flags_inner: ttk.Frame | None = None

        self._build_ui()
        self.reload_from_app()

    # ---------- проекты: история и настройки по папке ----------

    def _project_cfg(self, root: str | Path) -> dict:
        return (self._cfg_disk.get("projects") or {}).get(project_key(root), {})

    def _load_project_settings(self, root: str | Path) -> None:
        proj = self._project_cfg(root)
        self.app_dart_rel.set(proj.get("app_dart") or "lib/src/app.dart")
        self.pubspec_rel.set(proj.get("pubspec") or "pubspec.yaml")
        self.apk_name_var.set(proj.get("apk_name") or "")
        self.apk_suffix_var.set(proj.get("apk_suffix") or APK_SUFFIX_VERSION)

    def _recent_projects(self) -> list[str]:
        return list(self._cfg_disk.get("recent_projects") or [])

    def _remember_project(self, root: Path) -> None:
        key = project_key(root)
        recent = [p for p in self._recent_projects() if p != key]
        recent.insert(0, key)
        self._cfg_disk["recent_projects"] = recent[:RECENT_PROJECTS_LIMIT]
        self._cfg_disk["last_project"] = key
        save_config(self._cfg_disk)
        if hasattr(self, "root_combo"):
            self.root_combo.configure(values=self._cfg_disk["recent_projects"])

    def _switch_project(self, root: str) -> None:
        self.project_root.set(project_key(root))
        self._load_project_settings(root)
        self.reload_from_app()

    def _apk_suffix(self) -> str:
        return apk_suffix_value(
            self.apk_suffix_var.get(),
            int(self.major_var.get()),
            int(self.minor_var.get()),
            int(self.patch_var.get()),
            int(self.code_var.get()),
        )

    def _update_apk_preview(self) -> None:
        if not hasattr(self, "code_var"):
            return
        try:
            name = apk_file_name(self.apk_name_var.get(), self._apk_suffix())
            self.apk_preview_var.set(f"→ {name} (и {APK_DEFAULT_NAME})")
        except (ValueError, tk.TclError):
            self.apk_preview_var.set(f"→ только {APK_DEFAULT_NAME}")

    def _open_build_dir(self) -> None:
        root, _, _ = self._paths()
        folder = root / APK_OUTPUT_DIR
        if not folder.is_dir():
            messagebox.showinfo(
                "Сборка", f"Папки ещё нет — сначала соберите APK:\n{folder}"
            )
            return
        try:
            open_folder(folder)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _paths(self) -> tuple[Path, Path, Path]:
        return resolve_paths(
            {
                "project_root": self.project_root.get(),
                "app_dart": self.app_dart_rel.get(),
                "pubspec": self.pubspec_rel.get(),
            }
        )

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        paths = ttk.LabelFrame(root, text="Проект", padding=8)
        paths.pack(fill=tk.X)
        pr = ttk.Frame(paths)
        pr.pack(fill=tk.X)
        ttk.Label(pr, text="Корень").pack(side=tk.LEFT)
        # Выпадающий список — ранее открытые проекты.
        self.root_combo = ttk.Combobox(
            pr, textvariable=self.project_root, values=self._recent_projects()
        )
        self.root_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.root_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._switch_project(self.project_root.get()),
        )
        ttk.Button(pr, text="…", width=3, command=self._pick_root).pack(side=tk.LEFT)
        ar = ttk.Frame(paths)
        ar.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(ar, text="app.dart").pack(side=tk.LEFT)
        ttk.Entry(ar, textvariable=self.app_dart_rel).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )
        ttk.Button(paths, text="Прочитать из app.dart", command=self.reload_from_app).pack(
            anchor=tk.W, pady=(8, 0)
        )

        ver = ttk.LabelFrame(root, text="Версия (app.dart + pubspec)", padding=8)
        ver.pack(fill=tk.X, pady=(10, 0))
        row = ttk.Frame(ver)
        row.pack(fill=tk.X)
        self._version_spinner(row, "major", self.major_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        self._version_spinner(row, "minor", self.minor_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        self._version_spinner(row, "patch", self.patch_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        self._version_spinner(row, "code", self.code_var).pack(side=tk.LEFT)

        apk = ttk.LabelFrame(root, text="Имя APK (в конце — версия или номер сборки)", padding=8)
        apk.pack(fill=tk.X, pady=(10, 0))
        apk_row = ttk.Frame(apk)
        apk_row.pack(fill=tk.X)
        ttk.Label(apk_row, text="Имя").pack(side=tk.LEFT)
        ttk.Entry(apk_row, textvariable=self.apk_name_var, width=28).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Radiobutton(
            apk_row, text="версия", value=APK_SUFFIX_VERSION, variable=self.apk_suffix_var
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            apk_row, text="номер сборки", value=APK_SUFFIX_BUILD, variable=self.apk_suffix_var
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(apk, textvariable=self.apk_preview_var).pack(anchor=tk.W, pady=(6, 0))

        flags = ttk.LabelFrame(root, text="Флаги (из /// + const)", padding=8)
        flags.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        canvas = tk.Canvas(flags, highlightthickness=0)
        scroll = ttk.Scrollbar(flags, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._flags_inner = inner
        self._flags_canvas = canvas

        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(actions, text="Сохранить в app.dart", command=self.save_only).pack(
            side=tk.LEFT
        )
        self.build_btn = ttk.Button(
            actions,
            text="Сохранить и собрать APK (release)",
            command=self.save_and_build,
        )
        self.build_btn.pack(side=tk.LEFT, padx=8)
        ttk.Button(
            actions, text="Открыть папку сборки", command=self._open_build_dir
        ).pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(root, text="Лог сборки", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.log = tk.Text(log_frame, height=10, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

    def _pick_root(self) -> None:
        d = filedialog.askdirectory(
            initialdir=self.project_root.get() or str(default_project_root())
        )
        if d:
            self._switch_project(d)

    def _version_spinner(self, parent: ttk.Frame, title: str, var: tk.IntVar) -> ttk.Frame:
        box = ttk.Frame(parent)
        ttk.Label(box, text=title).pack()
        mid = ttk.Frame(box)
        mid.pack()
        ttk.Button(
            mid, text="−", width=3, command=lambda: var.set(max(0, var.get() - 1))
        ).pack(side=tk.LEFT)
        ttk.Entry(mid, textvariable=var, width=5, justify=tk.CENTER).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(mid, text="+", width=3, command=lambda: var.set(var.get() + 1)).pack(
            side=tk.LEFT
        )
        return box

    def _clear_flag_widgets(self) -> None:
        assert self._flags_inner is not None
        for w in self._flags_inner.winfo_children():
            w.destroy()
        self._flag_vars.clear()

    def _rebuild_flag_widgets(self, flags: list[FlagDef], saved_values: dict | None) -> None:
        self._clear_flag_widgets()
        assert self._flags_inner is not None
        saved = saved_values or {}
        for f in flags:
            row = ttk.Frame(self._flags_inner)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=f.label, width=42).pack(side=tk.LEFT)
            if f.kind == "bool":
                init = bool(saved.get(f.name, f.value))
                var: tk.Variable = tk.BooleanVar(value=init)
                ttk.Checkbutton(row, variable=var).pack(side=tk.LEFT)
            elif f.kind == "int":
                init_i = int(saved.get(f.name, f.value))
                var = tk.IntVar(value=init_i)
                ttk.Spinbox(row, from_=-10_000_000, to=10_000_000, textvariable=var, width=10).pack(
                    side=tk.LEFT
                )
            elif f.kind == "string":
                init_s = str(saved.get(f.name, f.value))
                var = tk.StringVar(value=init_s)
                ttk.Entry(row, textvariable=var, width=28).pack(side=tk.LEFT)
            elif f.kind == "enum":
                init_e = str(saved.get(f.name, f.value))
                var = tk.StringVar(value=init_e)
                opts = f.enum_options or [init_e]
                if init_e not in opts:
                    opts = [init_e, *opts]
                ttk.Combobox(
                    row,
                    textvariable=var,
                    values=opts,
                    state="readonly" if f.enum_options else "normal",
                    width=26,
                ).pack(side=tk.LEFT)
            else:
                continue
            self._flag_vars[f.name] = var
        self._flags = flags

    def reload_from_app(self) -> None:
        try:
            root, app, pub = self._paths()
            if not app.is_file():
                raise FileNotFoundError(f"Не найден {app}")
            src = read_text(app)
            ver = parse_version(src)
            flags = parse_flags(src, app)
            self.major_var.set(ver["major"])
            self.minor_var.set(ver["minor"])
            self.patch_var.set(ver["patch"])
            self.code_var.set(ver["versionCode"])
            if not self.apk_name_var.get().strip():
                default_name = pubspec_app_name(pub)
                self.apk_name_var.set(f"{default_name}_v" if default_name else "")
            # сохранённые значения флагов — только этого проекта
            disk_flags = self._project_cfg(root).get("flags") or {}
            self._rebuild_flag_widgets(flags, disk_flags)
            self._remember_project(root)
            self._append_log(
                f"Прочитано: {app.relative_to(root)} — "
                f"флагов {len(flags)}, версия "
                f"{ver['major']}.{ver['minor']}.{ver['patch']}+{ver['versionCode']}\n"
            )
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _collect_cfg(self) -> dict:
        flag_vals: dict = {}
        for f in self._flags:
            var = self._flag_vars.get(f.name)
            if var is None:
                continue
            raw = var.get()
            if f.kind == "bool":
                flag_vals[f.name] = bool(raw)
            elif f.kind == "int":
                flag_vals[f.name] = int(raw)
            else:
                flag_vals[f.name] = str(raw)
        return {
            "project_root": self.project_root.get(),
            "app_dart": self.app_dart_rel.get(),
            "pubspec": self.pubspec_rel.get(),
            "major": int(self.major_var.get()),
            "minor": int(self.minor_var.get()),
            "patch": int(self.patch_var.get()),
            "versionCode": int(self.code_var.get()),
            "flags": flag_vals,
            "apk_name": self.apk_name_var.get().strip(),
            "apk_suffix": self.apk_suffix_var.get(),
        }

    def save_only(self) -> None:
        try:
            self._write_files(self._collect_cfg())
            messagebox.showinfo("Готово", "app.dart и pubspec.yaml обновлены.")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def save_and_build(self) -> None:
        try:
            cfg = self._collect_cfg()
            named_apk = apk_file_name(
                cfg["apk_name"],
                apk_suffix_value(
                    cfg["apk_suffix"],
                    cfg["major"],
                    cfg["minor"],
                    cfg["patch"],
                    cfg["versionCode"],
                ),
            )
            root, _, _ = self._write_files(cfg)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return
        self.build_btn.configure(state=tk.DISABLED)
        self._append_log("\n=== flutter build apk --release ===\n")
        threading.Thread(
            target=self._run_build, args=(root, named_apk), daemon=True
        ).start()

    def _write_files(self, cfg: dict) -> tuple[Path, Path, Path]:
        root, app, pub = resolve_paths(cfg)
        if not app.is_file():
            raise FileNotFoundError(f"Не найден {app}")
        if not pub.is_file():
            raise FileNotFoundError(f"Не найден {pub}")
        src = read_text(app)
        # перечитать флаги из актуального файла (offsets)
        flags = parse_flags(src, app)
        # сверить имена
        unknown = set(cfg["flags"]) - {f.name for f in flags}
        if unknown:
            raise ValueError(f"Неизвестные флаги: {', '.join(sorted(unknown))}")
        out = apply_version(src, cfg)
        # offsets после apply_version могут сдвинуться — перепарсить
        flags = parse_flags(out, app)
        out = apply_flag_values(out, flags, cfg["flags"])
        write_text(app, out)
        write_text(pub, apply_pubspec(read_text(pub), cfg))
        projects = self._cfg_disk.setdefault("projects", {})
        projects[project_key(root)] = {
            "app_dart": cfg["app_dart"],
            "pubspec": cfg["pubspec"],
            "flags": cfg["flags"],
            "apk_name": cfg["apk_name"],
            "apk_suffix": cfg["apk_suffix"],
        }
        self._remember_project(root)
        ver = f"{cfg['major']}.{cfg['minor']}.{cfg['patch']}+{cfg['versionCode']}"
        self._append_log(
            f"Сохранено: {ver} → {app.name}, {pub.name}, {CONFIG_PATH.name}\n"
        )
        # обновить offsets в UI
        self._flags = parse_flags(out, app)
        return root, app, pub

    def _run_build(self, root: Path, named_apk: str) -> None:
        try:
            proc = subprocess.Popen(
                ["flutter", "build", "apk", "--release"],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=sys.platform == "win32",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.after(0, self._append_log, line)
            code = proc.wait()
            if code != 0:
                self.after(
                    0, self._append_log, f"\nСборка завершилась с ошибкой (exit {code}).\n"
                )
                return
            # app-release.apk остаётся, рядом — копия с именем и версией.
            out_dir = root / APK_OUTPUT_DIR
            shutil.copy2(out_dir / APK_DEFAULT_NAME, out_dir / named_apk)
            done = (
                f"Сборка готова:\n{APK_OUTPUT_DIR / APK_DEFAULT_NAME}\n"
                f"{APK_OUTPUT_DIR / named_apk}"
            )
            self.after(0, self._append_log, f"\nСборка успешна (exit 0).\n{done}\n")
            self.after(0, lambda: messagebox.showinfo("APK", done))
        except Exception as e:
            self.after(0, self._append_log, f"\nОшибка запуска: {e}\n")
            self.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
        finally:
            self.after(0, lambda: self.build_btn.configure(state=tk.NORMAL))

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text)
        self.log.see(tk.END)


def main() -> None:
    app = BuilderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
