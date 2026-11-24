# Portable dataset artifacts

## The defect

Phase G published 22 rendered files and 66 gold artifacts. Two of the gold files were
named with the document type inline:

    gold/v1/doc_00007_ndyafme64g7ly__workbook:shareholder_register__xlsx.json
    gold/v2/doc_00007_ndyafme64g7ly__workbook:shareholder_register__xlsx.json

On Windows a colon in a path is not a filename character: it introduces an **NTFS
alternate data stream**. Writing that path created

* a zero-byte regular file `doc_00007_ndyafme64g7ly__workbook`, and
* a 23,677-byte stream named `shareholder_register__xlsx.json` attached to it.

Everything passed at the time — Python's `open()` resolves the same path — but the
JSON was invisible to normal tooling (`ls`, `unzip`, git, most editors) and would be
lost by any copy to a non-NTFS filesystem, including a zip, a network share, or a
Linux/macOS checkout. The 66 artifact hashes verified because the reads went through
the stream.

Confirmed with:

```bash
py -3.13 -c "from pathlib import Path; p = Path('.../doc_00007_..._workbook'); \
print(p.stat().st_size, 'bytes'); print(Path(str(p)+':shareholder_register__xlsx.json').stat().st_size, 'stream bytes')"
# 0 bytes
# 23677 stream bytes
```

## The rule

`synthetic_data/paths.py` owns the mapping from canonical ids to filenames.

* Only `[A-Za-z0-9._-]` survives; every other character becomes `_`, so `:` can never
  reach the filesystem.
* Trailing spaces and dots are stripped from the source **before** substitution, so a
  name never ends with a space, a dot, or a meaningless `_` left by a stripped space.
* Reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with
  or without an extension) are escaped with a leading `_`.
* Empty components and `.`/`..` are refused.
* Long components are shortened to 180 characters with a ten-character hash suffix, so
  two long ids never collapse onto one name.
* `PortablePaths` checks every name it hands out for **case-insensitive** collisions on
  the full relative path, because Windows and macOS would otherwise overwrite one file
  with another. Collisions raise `PathCollision`.

Canonical ids are never rewritten: `doc:00007:ndyafme64g7ly` and
`workbook:shareholder_register` stay inside the JSON, which is where identity belongs.

## The repair

The pilot was republished, not overwritten:

```bash
py -3.13 -c "
from pathlib import Path
from synthetic_data.portable import rebuild_portable, archive_portable
src = Path('synthetic_data/output/v3_pilot')
dst = Path('synthetic_data/output/v3_pilot_portable')
rebuild_portable(src, dst)
archive_portable(dst, dst.parent / 'v3_pilot_portable.zip')
"
```

`rebuild_portable` copies bytes verbatim, checks each file against the source
manifest's digest, refuses empty or missing files, and writes
`manifest_portable.json` with relative paths, hashes and the rename map.

Result:

| role | files | content |
|---|---|---|
| gold v1 | 22 | byte-identical to Phase G |
| gold v2 | 22 | byte-identical to Phase G |
| alignment | 22 | byte-identical to Phase G |
| rendered | 22 | byte-identical to Phase G |
| equivalence fixtures | 3 | copied unchanged |

24 files were renamed (22 alignment, 2 workbook gold). No content changed.

## Archive verification

`archive_portable` writes a deterministic zip (sorted entries, fixed 1980 timestamps,
so the same tree always yields the same SHA-256). `verify_archive` extracts it to a
temporary directory and compares every relative path and hash, and rejects entries that
contain a colon or end with a space or dot.

```bash
py -3.13 -c "
import json
from pathlib import Path
from synthetic_data.portable import verify_archive
dst = Path('synthetic_data/output/v3_pilot_portable')
manifest = json.loads((dst/'manifest_portable.json').read_text(encoding='utf-8'))
print(verify_archive(dst.parent/'v3_pilot_portable.zip',
                     {entry['path']: entry['sha256'] for entry in manifest['files']}))
"
# {'checked': 91, 'expected': 91, 'problems': [], 'ok': True}
```

91 files (22 + 22 + 22 + 22 + 3) plus the manifest itself.

## Which directory to use

* `synthetic_data/output/v3_pilot/` — the Phase G original. Read it; do not
  redistribute it. Its two workbook gold files are alternate data streams.
* `synthetic_data/output/v3_pilot_portable/` — the portable copy. Use this for
  evaluation, archives and anything that leaves this machine.
* `synthetic_data/output/v3_pilot_portable.zip` — the distributable archive.

The Phase H QA artifacts and the Phase I evaluator read the portable directory.

## Tests

`test_v3_portable_paths.py` covers the naming rules (invalid characters, trailing
spaces and dots, reserved names, long-name uniqueness, case-insensitive collisions) and
the rebuild/archive round trip: 22/22/22 gold files present and non-empty, hashes equal
to Phase G, no colon in any path, relative manifest paths, byte-deterministic archive.
