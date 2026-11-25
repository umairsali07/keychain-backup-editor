# irestore patches

`KeychainBackupEditor` ships a patched build of
[`irestore`](https://github.com/dnicolson/irestore). The patches fix a bug that
made **modified** backups fail to restore, and add a way to pass the password
once. Apply these on top of the upstream fork and rebuild
(`go build ./cmd/irestore`).

The driver swap builds a **pure‑Go** binary, so no C compiler is needed on
Windows.

---

## 1. The real bug: stale `Size` in `Manifest.db` → "backup is corrupted"

Every file in an iOS backup has a record in `Manifest.db` (an encrypted SQLite
database). That record stores the file's **`Size`** and a SHA‑1 **`Digest`**.
When you delete keychain items, the `keychain-backup.plist` file gets smaller,
but `irestore` left the old, larger `Size` in the manifest. On restore, iTunes
reads `Size` bytes, runs past the end of the smaller file, and reports
**"backup is corrupted."**

(A no‑change rebuild kept the size identical, so it worked. A rebuild that *grew*
the file also happened to work, because a too‑small `Size` just reads fewer
bytes. Only a delete — a too‑large `Size` — failed.)

### Fix A — actually persist the update

`applyManifestUpdates` read the database back **before** the SQLite write was
flushed, so the new `Size` never made it to disk. Force a rollback journal and
close the DB before reading it:

```go
db, err := sql.Open("sqlite", tmpName)
if err != nil {
    return nil, err
}
// Commit each UPDATE straight into the main file (no unmerged -wal).
if _, err = db.Exec("PRAGMA journal_mode=DELETE"); err != nil {
    db.Close()
    return nil, err
}

for _, update := range updates {
    // ... existing QueryRow / patchManifestBlob / Exec ...
}

// Flush before reading the file back.
if err = db.Close(); err != nil {
    return nil, err
}
return os.ReadFile(tmpName)
```

### Fix B — update `Size` (and `Digest`) only when the file changed

In `unrestore`, record a manifest update when size, mtime, or digest changed,
and carry the new digest too:

```go
digest := sha1.Sum(encrypted)
sizeChanged := int64(len(plain)) != int64(rec.Length)
mtimeChanged := recordModTime(rec) > 0 && modTime != recordModTime(rec)
digestChanged := !bytes.Equal(digest[:], rec.Digest)
if sizeChanged || mtimeChanged || digestChanged {
    updates = append(updates, manifestUpdate{
        FileID: rec.HashCode(),
        Size:   int64(len(plain)),
        MTime:  modTime,
        Digest: digest[:],
    })
}
```

`manifestUpdate` gains a `Digest []byte` field, `patchManifestBlob` sets the
`Digest` object in the NSKeyedArchiver blob (via a small `setArchiveBytesField`
helper), and `crypto/sha1` is imported. Leaving unchanged files alone keeps a
no‑op rebuild byte‑identical.

> Note: iTunes does not appear to verify `Digest` (a grow‑edit with a stale
> digest still restored), but it is updated for correctness anyway. `Size` is
> the field that actually matters.

---

## 2. One‑time password via environment variable

So a GUI can supply the password once instead of being prompted on every
command, `getpass()` checks an environment variable first:

```go
func getpass() string {
    if env := os.Getenv("IRESTORE_PASSWORD"); env != "" {
        return env // password, or the pre-derived hex key to skip derivation
    }
    fmt.Fprint(os.Stderr, "Backup Password: ")
    pw, err := terminal.ReadPassword(int(syscall.Stdin))
    must(err)
    fmt.Println()
    return string(pw)
}
```

---

## 3. Pure‑Go SQLite driver (no C compiler)

Swap the CGO driver for `modernc.org/sqlite` so the binary builds with
`CGO_ENABLED=0`:

- `backup/backup.go`: `_ "github.com/mattn/go-sqlite3"` → `_ "modernc.org/sqlite"`
- both `sql.Open("sqlite3", …)` → `sql.Open("sqlite", …)`
- `go get modernc.org/sqlite && go mod tidy`

Build:

```bat
set CGO_ENABLED=0
go build -o irestore.exe ./cmd/irestore
```
