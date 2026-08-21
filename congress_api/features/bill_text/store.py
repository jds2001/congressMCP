"""The persistence path: build a package file, publish it atomically, serve
from it (spec §10).

    BillTextIndex(parsed, conn=create_package_db(temp))   # build into a temp
    -> close -> validate_package_file(temp)                # close-and-validate
    -> publish_package(temp, final)                        # os.replace; loser adopts
    -> BillTextIndex.from_connection(open read-only)       # serve the PUBLISHED file

Every consumer of a package file goes through ``PackageStore`` so the four
steps above are the only way a file reaches its final name (§10: adoption is
safe only because a file at its final name is complete by construction).

Manifest writes here are best-effort and never fail the user's call: the
filesystem is authoritative, the manifest is derived (§10).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import cache
from .index import FTS_TABLE, PACKAGE_TABLES, BillTextIndex
from .parser import ParsedBill

logger = logging.getLogger(__name__)

SOURCE_FORMAT = "bill_dtd"


class PackageBuildError(Exception):
    """A freshly built package failed close-and-validate; nothing was published."""


@dataclass(frozen=True)
class _Stamp:
    mtime_ns: int
    size: int
    ino: int


def _stamp(path: Path) -> _Stamp | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return _Stamp(st.st_mtime_ns, st.st_size, st.st_ino)


class PackageStore:
    """Package files under one cache root.

    ``fresh_path`` / ``open`` answer "is there a servable file for this package
    at this source lastModified"; ``build_and_publish`` turns a ParsedBill into
    one. A validated file is remembered by its stat signature so the seven-rule
    check (which includes an FTS integrity-check) runs once per distinct file,
    not on every hit.
    """

    def __init__(self, settings: cache.CacheSettings):
        self.settings = settings
        self.layout = settings.layout
        self._validated: dict[Path, tuple[_Stamp, str]] = {}
        self._manifest: cache.Manifest | None = None

    # -- manifest (derived; best-effort) ------------------------------------

    def manifest(self) -> cache.Manifest | None:
        if self._manifest is None:
            try:
                self._manifest = cache.Manifest(self.layout.manifest_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("manifest unavailable: %s", exc)
                return None
        return self._manifest

    def _record_publish(self, package_id: str, path: Path, last_modified: str | None) -> None:
        manifest = self.manifest()
        if manifest is None:
            return
        try:
            now = time.time()
            manifest.upsert(
                cache.ManifestRow(
                    package_id=package_id,
                    filename=path.name,
                    schema_version=cache.SCHEMA_VERSION,
                    bytes=path.stat().st_size,
                    created_at=now,
                    last_accessed_at=now,
                    source_format=SOURCE_FORMAT,
                    source_last_modified=last_modified,
                )
            )
        except Exception as exc:
            logger.warning("manifest upsert failed for %s: %s", package_id, exc)

    def _record_hit(self, package_id: str) -> None:
        manifest = self.manifest()
        if manifest is None:
            return
        try:
            manifest.touch(package_id)
        except Exception as exc:
            logger.warning("manifest touch failed for %s: %s", package_id, exc)

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()
            self._manifest = None

    # -- lookup ---------------------------------------------------------------

    def _validate(self, path: Path, package_id: str) -> cache.PackageValidation:
        stamp = _stamp(path)
        if stamp is None:
            return cache.PackageValidation(False, "missing", None, False, {})
        remembered = self._validated.get(path)
        if remembered is not None and remembered[0] == stamp:
            return cache.PackageValidation(True, None, cache.SCHEMA_VERSION, False, {"source_last_modified": remembered[1]})
        verdict = cache.validate_package_file(
            path,
            expected_package_id=package_id,
            expected_tables=PACKAGE_TABLES,
            fts_tables=(FTS_TABLE,),
        )
        if verdict.ok:
            self._validated[path] = (stamp, verdict.meta.get("source_last_modified", ""))
        else:
            self._validated.pop(path, None)
        return verdict

    def fresh_path(self, package_id: str, last_modified: str | None) -> Path | None:
        """The published file for ``package_id`` if it validates and was built
        from the same source ``lastModified``. A file built from a different
        lastModified is a reissue (§10 freshness table: discard and rebuild); a
        file failing validation is skipped, and unlinked only where §10 allows.
        """
        path = self.layout.package_path(package_id)
        if not path.exists():
            return None
        verdict = self._validate(path, package_id)
        if not verdict.ok:
            logger.info("package %s not servable: %s", path.name, verdict.reason)
            if verdict.unlink_advised:
                self._unlink(path, package_id)
            return None
        if last_modified is not None and verdict.meta.get("source_last_modified", "") != last_modified:
            logger.info(
                "package %s was built from lastModified %r, source now %r; discarding",
                path.name, verdict.meta.get("source_last_modified"), last_modified,
            )
            self._unlink(path, package_id)
            return None
        return path

    def open(self, package_id: str, last_modified: str | None) -> BillTextIndex | None:
        """Serve ``package_id`` from its published file, or ``None`` on a miss."""
        path = self.fresh_path(package_id, last_modified)
        if path is None:
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            index = BillTextIndex.from_connection(conn)
        except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
            # Vanished or broke between validation and open: a miss, never an error.
            logger.warning("could not open package %s: %s", path.name, exc)
            self._validated.pop(path, None)
            return None
        self._record_hit(package_id)
        return index

    # -- build + publish -------------------------------------------------------

    def build_and_publish(self, parsed: ParsedBill, *, last_modified: str | None) -> tuple[BillTextIndex, bool]:
        """Build ``parsed`` into a temp file, close-and-validate, publish, and
        return the index SERVED FROM THE PUBLISHED FILE plus whether this call
        was the publisher. If another process published first, our temp is
        discarded and theirs is served (the loser rule). Raises
        ``PackageBuildError`` if the result cannot be served; the caller falls
        back to an in-memory index."""
        package_id = parsed.package_id
        self.layout.ensure_dirs()
        temp = self.layout.temp_path(package_id)
        conn = cache.create_package_db(temp)
        try:
            BillTextIndex(parsed, conn=conn)
            cache.write_package_meta(
                conn,
                package_id=package_id,
                source_format=SOURCE_FORMAT,
                source_last_modified=last_modified,
                build_complete=True,
            )
        except Exception:
            conn.close()
            _discard(temp)
            raise
        conn.close()  # closed BEFORE validation and publication; no journal may remain

        verdict = cache.validate_package_file(
            temp,
            expected_package_id=package_id,
            expected_tables=PACKAGE_TABLES,
            fts_tables=(FTS_TABLE,),
        )
        if not verdict.ok:
            _discard(temp)
            raise PackageBuildError(f"fresh build of {package_id} failed validation: {verdict.reason}")

        final = self.layout.package_path(package_id)
        path, published = cache.publish_package(temp, final)
        if published:
            self._record_publish(package_id, path, last_modified)
        else:
            logger.info("package %s was published concurrently; adopting the existing file", final.name)

        index = self.open(package_id, last_modified)
        if index is None:
            # The file at the final name (ours or the winner's) is not servable.
            raise PackageBuildError(f"published package {final.name} is not servable")
        return index, published

    # -- helpers ------------------------------------------------------------------

    def _unlink(self, path: Path, package_id: str) -> None:
        self._validated.pop(path, None)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Windows: held open elsewhere. Skip; the file stays a non-servable miss.
            logger.warning("could not unlink %s: %s", path.name, exc)
            return
        manifest = self.manifest()
        if manifest is not None:
            try:
                manifest.remove(package_id)
            except Exception as exc:
                logger.warning("manifest remove failed for %s: %s", package_id, exc)


def _discard(path: Path) -> None:
    for candidate in (path, path.with_name(path.name + "-journal")):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove %s: %s", candidate, exc)
