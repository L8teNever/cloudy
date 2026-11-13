"""Pakete entpacken: viele Dateien in einem Rutsch hochladen (Browser packt sie als TAR) und ZIP/TAR im Ordner entpacken.

Viele kleine Dateien einzeln zu übertragen kostet pro Datei Zeit. Als ein Paket gehen sie mit vollem Tempo
über die Leitung und werden auf dem Server direkt in den Ordner geschrieben (ohne Zwischenkopie des Pakets).
"""
import io
import os
import queue
import secrets
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

CHUNK = 1024 * 1024
MAX_GEMELDET = 5000  # so viele übersprungene Pfade gehen höchstens an den Browser zurück


class PaketFehler(RuntimeError):
    pass


@dataclass
class Ergebnis:
    ok: int = 0
    uebersprungen: list[str] = field(default_factory=list)  # gab es schon (nicht ersetzt)
    fehler: list[str] = field(default_factory=list)


class Rohr(io.RawIOBase):
    """Datei-Objekt, das aus einer Warteschlange liest (Hochladen läuft async, Entpacken im Thread)."""

    def __init__(self, tiefe: int = 32):
        self.q: queue.Queue = queue.Queue(maxsize=tiefe)
        self._buf = b""
        self._ende = False

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf and not self._ende:
            chunk = self.q.get()
            if chunk is None:
                self._ende = True
            else:
                self._buf = chunk
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _teile(name: str) -> list[str] | None:
    """Sicherer relativer Pfad aus dem Paket, sonst None."""
    name = name.replace("\\", "/")
    teile = [p for p in name.split("/") if p and p != "."]
    if not teile or any(p == ".." or "\0" in p or len(p.encode()) > 255 for p in teile) or name.startswith("/"):
        return None
    if teile[0] == ".mergerfs":
        return None
    return teile


class Schreiber:
    """Schreibt Einträge sicher unter `ordner` (nie außerhalb von `root`)."""

    def __init__(self, root: Path, ordner: Path, ersetzen: bool, besitzer: Callable[[Path], None],
                 neu_datei: Callable[[Path], None] = lambda p: None):
        self.root = root.resolve()
        self.ordner = ordner
        self.ersetzen = ersetzen
        self.besitzer = besitzer
        self.neu_datei = neu_datei
        self.erg = Ergebnis()

    def _innen(self, p: Path) -> bool:
        r = p.resolve()
        return r == self.root or self.root in r.parents

    def _mkdirs(self, teile: list[str]) -> Path:
        p = self.ordner
        for t in teile:
            p = p / t
            if not p.exists():
                p.mkdir()
                self.besitzer(p)
            if not p.is_dir() or not self._innen(p):
                raise PaketFehler(f"„{'/'.join(teile)}“ ist kein Ordner")
        return p

    def ordner_anlegen(self, name: str) -> None:
        teile = _teile(name)
        if teile is None:
            self.erg.fehler.append(f"{name}: ungültiger Pfad")
            return
        try:
            self._mkdirs(teile)
        except PaketFehler as exc:
            self.erg.fehler.append(str(exc))

    def datei(self, name: str, quelle: BinaryIO) -> None:
        teile = _teile(name)
        if teile is None:
            self.erg.fehler.append(f"{name}: ungültiger Pfad")
            return
        rel = "/".join(teile)
        try:
            parent = self._mkdirs(teile[:-1])
        except PaketFehler as exc:
            self.erg.fehler.append(str(exc))
            return
        ziel = parent / teile[-1]
        if ziel.is_symlink() or ziel.is_dir():
            self.erg.fehler.append(f"{rel}: ist ein Ordner")
            return
        if ziel.exists() and not self.ersetzen:
            if len(self.erg.uebersprungen) < MAX_GEMELDET:
                self.erg.uebersprungen.append(rel)
            return
        tmp = parent / f".{teile[-1][:200]}.hochladen-{secrets.token_hex(4)}"
        try:
            with open(tmp, "wb") as dst:
                shutil.copyfileobj(quelle, dst, CHUNK)
            self.besitzer(tmp)
            os.replace(tmp, ziel)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.erg.ok += 1
        self.neu_datei(ziel)


def tar_entpacken(fileobj: BinaryIO, s: Schreiber, strom: bool = True) -> Ergebnis:
    """TAR (auch .tar.gz usw.) entpacken. strom=True: aus einem Datenstrom ohne Zurückspulen."""
    try:
        with tarfile.open(fileobj=fileobj, mode="r|*" if strom else "r:*") as tf:
            for m in tf:
                if m.isdir():
                    s.ordner_anlegen(m.name)
                elif m.isfile():
                    src = tf.extractfile(m)
                    if src is not None:
                        s.datei(m.name, src)
                # Links, Geräte usw. werden bewusst ausgelassen
    except tarfile.TarError as exc:
        if not s.erg.ok:
            raise PaketFehler(f"Kein gültiges Paket: {exc}") from exc
        s.erg.fehler.append(f"Paket unvollständig: {exc}")
    return s.erg


def zip_entpacken(path: Path, s: Schreiber) -> Ergebnis:
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename
                if info.is_dir():
                    s.ordner_anlegen(name)
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue  # Symlink
                with zf.open(info) as src:
                    s.datei(name, src)
    except (zipfile.BadZipFile, RuntimeError) as exc:  # RuntimeError: verschlüsselt
        raise PaketFehler(f"ZIP lässt sich nicht öffnen: {exc}") from exc
    return s.erg


def ist_paket(name: str) -> bool:
    n = name.lower()
    return n.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"))


def ordner_name(name: str) -> str:
    for end in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".zip", ".tar"):
        if name.lower().endswith(end):
            return name[: -len(end)] or "Entpackt"
    return name


def entpacken(path: Path, s: Schreiber) -> Ergebnis:
    if path.name.lower().endswith(".zip"):
        return zip_entpacken(path, s)
    with open(path, "rb") as fh:
        return tar_entpacken(fh, s, strom=False)
