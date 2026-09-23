"""Explicit Windows tool setup; runtime ingestion never downloads executables."""
from pathlib import Path
import hashlib
import json
import os
import platform
import zipfile
from .storage import staging_directory

VERSION = "0.14"
URL = f"https://github.com/LibreDWG/libredwg/releases/download/{VERSION}/libredwg-{VERSION}-win64.zip"
SHA256 = "1ad7e15344d20b3426c3435b078d82fb84b35062815946b2cca9c5fc9810fea8"


def prepare(data_dir: Path) -> dict:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("自動安裝支援 Windows x64；其他平台請安裝 dwg2dxf 並設定 RAG_DWG2DXF")
    import requests
    tools = data_dir / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    destination = tools / f"libredwg-{VERSION}"
    if (destination / "installation.json").is_file():
        return json.loads((destination / "installation.json").read_text(encoding="utf-8"))
    with staging_directory(tools, "dwg-setup") as folder:
        archive = folder / "download.zip"
        with requests.get(URL, stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            with archive.open("wb") as stream:
                for piece in response.iter_content(1024 * 1024):
                    stream.write(piece)
                    if stream.tell() > 30_000_000:
                        raise ValueError("轉檔工具下載大小不符")
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
                raise ValueError("轉檔工具 SHA-256 不符，未安裝")
        extracted = folder / "unpacked"
        with zipfile.ZipFile(archive) as package:
            if sum(info.file_size for info in package.infolist()) > 200_000_000:
                raise ValueError("轉檔工具解壓縮大小不符")
            for info in package.infolist():
                if not (extracted / info.filename).resolve().is_relative_to(extracted.resolve()):
                    raise ValueError("轉檔工具封裝路徑錯誤")
            package.extractall(extracted)
        executable = next(extracted.rglob("dwg2dxf.exe"))
        manifest = {"tool": "GNU LibreDWG", "version": VERSION, "license": "GPL-3.0-or-later",
                    "download": URL, "sha256": SHA256,
                    "executable": str(destination / executable.relative_to(extracted))}
        (extracted / "installation.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if not destination.resolve().is_relative_to(tools.resolve()) or not extracted.resolve().is_relative_to(tools.resolve()):
            raise ValueError("轉檔工具安裝路徑超出資料目錄")
        extracted.rename(destination)
    return manifest
