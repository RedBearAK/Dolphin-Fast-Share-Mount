# Dolphin-Fast-Share-Mount

A right-click action for Dolphin (KDE Plasma 5.22+ and 6) that turns the `smb://` share you are browsing into a real kernel CIFS mount and opens it.

You get two things Dolphin's own `smb://` browsing does not give you:

- **A real folder path.** Files on the share can be dragged into web browsers, upload dialogs and any other program that only understands local paths.
- **Kernel speed.** Dolphin reads `smb://` through KIO's user-space SMB client, which on gigabit typically manages 40–60 MiB/s and wanders. The kernel CIFS client fills the link (around 110 MiB/s) in both directions, and large uploads to the share are where the difference shows most.

Mounts are transient. Nothing is added to fstab, nothing runs at boot, and nothing is left after a reboot. There is no daemon.

If you want the opposite, permanent mounts of network shares that come back after a reboot, have a look at [netmnt](https://github.com/thongor77/netmnt), which adds persistent mounting to Dolphin's context menu.

## How it looks

Right-click a folder, or the empty background of a folder, in an `smb://` location:

- **Fast Share Mount → Mount Here** mounts the share and opens the same folder on the mount, in a new tab (or wherever your Dolphin settings open external folders).
- **Fast Share Mount → Mount Here in New Window** does the same in a new window.

With several shares selected, each is mounted, and the server's folder opens, listing them all (one folder per server). Used in a server's own folder (`smb://nas.local/`), the entry lists that server's shares, as Dolphin sees them with your saved login, and mounts the ones you tick; shares already mounted are marked.

When kio-fuse is running, Dolphin may hand the menu a kio-fuse folder (`/run/user/<uid>/kio-fuse-…/smb/…`) instead of the `smb://` address; the tool asks kio-fuse which address the folder stands for and mounts that.

Right-click anything, anywhere (a file, a folder or the background, local or remote):

- **Fast Share Unmount…** lists the shares the tool has mounted for you, with the one you clicked in already ticked. Tick one or more and press OK. A share that is still in use (an open file, a terminal inside it) is reported, with the offer to detach it anyway. With nothing mounted you get a notification instead of the list.

Dolphin's sidebar lists each mount under Remote. Its own **Unmount** (in the entry's context menu) does not work and says "must be superuser to unmount": the mount belongs to root, and the system lets a normal user unmount only what fstab allows. Use **Fast Share Unmount…** instead.

Both entries sit in the main context menu, next to each other (`X-KDE-Priority=TopLevel`), so they never end up in Dolphin's **Actions** submenu, which collects service menu entries once more than four apply. They also do not count towards those four.

Every outcome is a desktop notification, and every failure is also a dialog that names the cause: wrong password, an account or share the server refuses, share not found, server not reachable, a missing component with the command that installs it, or a dismissed authorisation request.

The mount appears at:

```
/run/dolphin-fast-share-mount/<your uid>/<server address>/<share>@<server display name>
```

for example `/run/dolphin-fast-share-mount/1000/photos.local/Backups@Photos`. Dolphin's sidebar lists the mount under the same name, with a network-folder icon. Both names can be changed in the settings (`folder_name`, `sidebar_name`, below), from these parts:

| Placeholder | Meaning | Example |
|---|---|---|
| `{share}` | the share | `Backups` |
| `{server_address}` | the server as in the address | `photos.local`, `photos.example.net`, `192.168.1.20` |
| `{server_display_name}` | the name the server announces on the network, the one Dolphin's network view shows (with its own capitals); the short name when it announces none | `Photos` |
| `{server_short_name}` | the first part of the server's name; an IP address stays whole | `photos` |

`@` needs no quoting in a shell and is a plain character in file paths and `file://` addresses. Brackets, parentheses and spaces would need quoting in a shell (zsh refuses `[...]` unquoted), so the folder name keeps to letters, digits and `. _ - + @`; the sidebar name may be anything.

One folder per server keeps shares with generic names (`Shared`, `Media`, `Backups`) on different servers apart. In the folder name, every byte other than an ASCII letter, a digit, `.`, `_`, `-`, `+` or `@` becomes `_` (so `My Share` becomes `My_Share`, and `Ö`, two bytes in UTF-8, becomes `__`), a leading `.` becomes `_`, and names are cut at 100 bytes. If two shares end up with the same folder name, the second gets `~2`, the next `~3`, up to `~9`.

## Requirements

- KDE Plasma 5.22 or later (KDE Frameworks 5.85+) or Plasma 6, with Dolphin
- `mount.cifs` (package `cifs-utils`)
- `setfacl` (package `acl`)
- `/usr/bin/python3` 3.10 or later (standard library only)
- `pkexec` (polkit), or `sudo` with `ksshaskpass`
- `kdialog` (or `zenity`) for the password prompt and dialogs
- `gdbus` (glib2 tools), to find the address behind a kio-fuse folder and to use KWallet
- Optional: `kioclient` (KIO) to pick shares on a server; `notify-send` for notifications; KWallet (`kwallet-query`) or libsecret (`secret-tool`) to remember passwords

On openSUSE:

```
sudo zypper install cifs-utils acl kdialog libnotify-tools kwallet-tools glib2-tools
```

## Install

```
git clone https://github.com/RedBearAK/Dolphin-Fast-Share-Mount.git
cd Dolphin-Fast-Share-Mount
./install.sh
```

Run it as yourself, not with sudo. It asks for root rights once, for the system part, and installs:

| Part | Where |
|---|---|
| Command `fast-share-mount` | `~/.local/bin/` |
| Dolphin menus | `~/.local/share/kio/servicemenus/` |
| Settings template (only if you have none) | `~/.config/dolphin-fast-share-mount.conf` |
| Uninstaller copy and file list | `~/.local/share/dolphin-fast-share-mount/` |
| Root helper | `/usr/local/libexec/fast-share-mount-helper` |
| polkit action | `/usr/share/polkit-1/actions/io.github.redbearak.dolphin-fast-share-mount.policy` |
| Uninstaller copy and file list | `/usr/local/share/dolphin-fast-share-mount/` |

Running `./install.sh` again repairs an installation: every file is written again with the right permissions, and files an older version installed but this one no longer uses are removed. Your settings file is never overwritten.

Afterwards, check what the tool found on your system:

```
fast-share-mount check
```

## Uninstall

```
~/.local/share/dolphin-fast-share-mount/uninstall.sh
```

(or `./uninstall.sh` from the project folder). It unmounts the tool's shares first, then removes everything the installer put in place, the log, your settings and the passwords the tool saved. Use `--keep-config` and `--keep-passwords` to keep those. If a share is still in use it stops and says which; `--force` detaches it anyway. Running it twice is harmless.

## Command line

```
fast-share-mount mount [--tab|--window|--no-open] [--user NAME] [--guest] [--vers VERSION] URL...
fast-share-mount unmount [--force] PATH|URL|SERVER/SHARE...
fast-share-mount unmount --all
fast-share-mount unmount --choose [PATH...]
fast-share-mount list
fast-share-mount check
fast-share-mount forget 'USER@SERVER'
fast-share-mount forget 'DOMAIN;USER@SERVER'
fast-share-mount forget --all
fast-share-mount --version
```

Examples:

```
fast-share-mount mount smb://nas.local/Media/Films
fast-share-mount mount 'smb://CORP;jordan@fileserver/Projects'
fast-share-mount mount smb://guest@printer-box/Scans
fast-share-mount unmount nas.local/Media
```

The user name comes from `--user`, else from the address (`smb://user@server/share`), else from the settings file, else your login name. Quote addresses that contain `;` in a shell. `smb://guest@...` or `--guest` connects without a password. In a terminal, the command prints its messages and prompts there; from the Dolphin menu it uses notifications and dialogs.

## Settings

`~/.config/dolphin-fast-share-mount.conf`, one `key=value` per line. The file is read, never executed.

| Setting | Values | Default |
|---|---|---|
| `user` | user name for addresses that name none | your login name |
| `domain` | Windows domain or workgroup | none |
| `smb_version` | `3.1.1`, `3.0.2`, `3.0`, `3`, `2.1`, `2.0`, `default`, `1.0` | empty: the kernel picks the newest version both sides speak (2.1 or later) |
| `open_mode` | `tab`, `window`, `none` | `tab` |
| `remember_password` | `ask`, `always`, `never` | `ask` |
| `folder_name` | a pattern with `{share}` (required) and any of the server placeholders above, plus letters, digits and `. _ - + @`; `share` and `server-share` are short for `{share}` and `{server_display_name}-{share}` | `{share}@{server_display_name}` |
| `sidebar_name` | `folder_name` (the folder's own name), or quoted text with the same placeholders, such as `"{share} [{server_display_name}]"`; longer than 40 characters, the middle becomes `…` | `folder_name` |

SMB1 (`1.0`) is never used unless you set it here or pass `--vers 1.0`, because it is insecure. A server that only speaks SMB1 produces an error that says so.

## How it works, and why it is safe

Mounting needs root, so the tool has two halves.

`fast-share-mount` runs as you. It reads the address Dolphin passes, checks whether the share is already mounted (by this tool, or by anything else such as fstab or Smb4K, in which case it opens that mount instead of stacking a second one), checks that the server answers on its SMB port, gets the password from KWallet or asks for it, and starts the helper through pkexec.

`fast-share-mount-helper` is a bash script, the only code that runs as root. It:

- validates every argument (server name, share name, user name, domain, port, SMB version), refusing anything that could add a mount option, such as a comma in a share name;
- computes every mount point itself; for unmounting it accepts only a path of exactly the shape `/run/dolphin-fast-share-mount/<uid>/<server>/<share>` that holds a CIFS mount;
- under pkexec, only mounts for the user who asked;
- receives the password on stdin and hands it to `mount.cifs` through a pipe, so it never appears in a process list, an environment or a file;
- keeps the whole tree under `/run/dolphin-fast-share-mount` owned by root, so no user can swap a folder for a symbolic link and redirect a mount; your own folder in it carries an access list that lets only you in (it shows as `drwxr-x---`, root's group and everyone else having no access), so other users on the computer cannot open your mounted files or list your folder. They can still see which servers and shares are mounted, and for which user name, because the system's mount table (`/proc/self/mountinfo`) is readable by everyone; `fast-share-mount list` does not show other users' mounts;
- mounts with `uid`/`gid` set to you, `noperm` (the server enforces permissions), `nosuid` and `nodev`;
- checks the mount is really there and readable before reporting success.

The user side only starts a helper that is owned by root in folders only root can change. The polkit action uses `auth_admin_keep`: one password prompt covers a few minutes of mounting and unmounting.

If you want to mount without a password prompt, a polkit rule can allow it for one group. This is not installed by default. As an example, `/etc/polkit-1/rules.d/49-fast-share-mount.rules`:

```
polkit.addRule(function(action, subject) {
    if (action.id == "io.github.redbearak.dolphin-fast-share-mount.mount" &&
        subject.active && subject.local && subject.isInGroup("wheel")) {
        return polkit.Result.YES;
    }
});
```

## Logs

Each attempt is logged to the journal and to `~/.local/state/dolphin-fast-share-mount/log`. To see both halves of an attempt:

```
journalctl -t fast-share-mount -t fast-share-mount-helper --since "10 min ago"
```

Set `FAST_SHARE_MOUNT_DEBUG=1` to log the commands run (passwords are never logged).

## Limitations

- Image-based systems such as Fedora Atomic (Kinoite) are not supported yet: `/usr/share` is read-only there and `/usr/local` is a symbolic link into `/var`.
- Shares stay mounted until you unmount them or reboot; there is no idle timeout.
- IPv6 address literals in the address (`smb://[fe80::1]/share`) are not supported yet; use the server's name.
- **Unmount** in the context menu of Dolphin's sidebar entry cannot unmount these shares; use **Fast Share Unmount…** from any context menu.
- Only SMB/CIFS. NFS may follow with the same helper design.

## Development

```
tests/run.sh
```

runs `bash -n`, `shellcheck` (when installed) and the unit tests (Python `unittest`, no other dependencies). The helper's functions are tested by sourcing it into bash, and its mount command runs against a stub `mount.cifs`, so none of this needs root.

`tests/integration/samba-test.sh` mounts real shares from a Samba container (podman or docker) and needs `cifs-utils` and sudo. GitHub Actions runs both.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
