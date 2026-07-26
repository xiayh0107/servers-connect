# Mounting a host locally

`serverctl mount` exposes a remote directory as a local folder through sshfs, so
Finder, Explorer, and your editor can open remote files directly. It reuses the
managed OpenSSH configuration and the same credential boundary as every other
connection — there is no second place to configure a password or a key.

```bash
serverctl mount web1:/srv/app          # mounts at ~/mnt/web1
serverctl mount web1:/srv/app ~/work   # or wherever you like
serverctl mount web1 --read-only       # the login directory, read-only
serverctl mount --list
serverctl unmount web1
```

## This one needs software we cannot install for you

Every other feature works with the OpenSSH client you already have. Mounting
does not: it needs a FUSE filesystem driver, and installing one requires
administrator rights and, on macOS, approving a system extension and rebooting.

Check first:

```bash
serverctl doctor
```

The `mount` row reports the backend or names the missing piece.

| Platform | What you need | Notes |
|---|---|---|
| macOS | [macFUSE](https://macfuse.io) **and** an sshfs binary | `brew install gromgit/fuse/sshfs-mac`. macFUSE is a system extension: expect an approval prompt in System Settings and a reboot. On Apple Silicon this also requires reduced security in Recovery. |
| Linux | `sshfs` and `fusermount3` | Packaged everywhere: `apt install sshfs`, `dnf install fuse-sshfs`, `pacman -S sshfs`. |
| Windows | [SSHFS-Win](https://github.com/winfsp/sshfs-win) and WinFsp | Install WinFsp first. Mount points are drive letters. |

Installing sshfs from Homebrew without macFUSE is the most common failure — the
binary exists, so it looks installed, and every mount fails. `doctor` reports
that case specifically.

## How it behaves

- The default mount point is `~/mnt/ALIAS`, created if missing. A directory that
  already has contents is refused rather than mounted over, so nothing you have
  locally can be hidden by a mount.
- `serverctl mount --list` reads the operating system's mount table, not a list
  we keep. Eject in Finder or run `umount` yourself and the output stays
  correct.
- Mounts are not restored after a reboot, and nothing is written to your
  startup files. If you want one to persist, that is your system's job — add it
  to your own login items or an automount configuration.
- `--read-only` mounts without write access. Use it whenever you are only
  reading; it removes the chance of an editor autosaving into production.
- Connections use `reconnect` and server keepalives, so a laptop that sleeps
  usually recovers. If a mount does wedge, unmount it explicitly:
  `fusermount3 -u ~/mnt/web1` on Linux, `umount ~/mnt/web1` on macOS.

## When not to mount

A mount makes remote files look local, which also makes remote latency look
like local latency. Anything that walks a large tree — a project-wide search, a
build, a language server indexing a repository — will be slow and may appear to
hang.

For those, prefer:

- `serverctl exec ALIAS -- <command>` to run the work where the files are.
- `serverctl get` / `put` for individual files.
- The web UI's file browser for looking around without mounting anything.

Mounting earns its place when you want to edit a handful of remote files in a
local editor, and little else.
