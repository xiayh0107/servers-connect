# Packaging outside PyPI

`pipx install ssh-server-manager` and `pip install ssh-server-manager` are the
supported installs; the wheel needs no Node.js, no build step and no network at
runtime. This page covers the two distribution channels that need extra work, and
is explicit about which of them this project can actually ship.

## Homebrew (a tap you host)

Homebrew cannot install a PyPI package from the main formula set unless the
project adopts it there, so the supported route is a tap:

```bash
brew tap-new YOURUSER/ssh-server-manager
scripts/prepare_packaging dist/ssh_server_manager-X.Y.Z.tar.gz --output dist/packaging
cp dist/packaging/homebrew/ssh-server-manager.rb \
   "$(brew --repository YOURUSER/ssh-server-manager)/Formula/"
brew update-python-resources YOURUSER/ssh-server-manager/ssh-server-manager
git -C "$(brew --repository YOURUSER/ssh-server-manager)" add -A
git -C "$(brew --repository YOURUSER/ssh-server-manager)" commit -m "ssh-server-manager X.Y.Z"
git -C "$(brew --repository YOURUSER/ssh-server-manager)" push
```

`packaging/homebrew/ssh-server-manager.rb.in` is the template and
`scripts/prepare_packaging` is the only thing that should fill it in: the
version and the sdist hash must match the artifact that was actually uploaded,
and the script refuses an sdist whose version disagrees with `__version__` or
whose archive does not contain `ssh_server_manager-<version>/`.

`brew update-python-resources` generates the dependency `resource` blocks. Those
pin a hash per dependency, so they are produced in the tap (where network access
is expected) rather than committed here.

## winget (not planned)

winget installs a Windows installer or a portable archive, not a Python package.
Shipping a winget manifest would mean building and signing an `.exe`/`.msi` (or a
zip with a launcher) and maintaining that build for every release — a second
packaging pipeline for a tool whose Windows install already works:

```powershell
irm https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.ps1 | iex
```

or `pipx install ssh-server-manager`. Until a Windows installer actually exists,
publishing a manifest would promise an install path that is not tested, so this
stays out of the roadmap.

## What is *not* packaged

- No Docker image: the tool's value is this machine's keychain and SSH agent, and
  a container has neither.
- No snap/flatpak: confinement hides `~/.ssh` and the OS credential store, which
  are the two things the tool exists to use.

## Checklist addition

After `python -m build --outdir dist .` and the wheel smoke test, run
`scripts/prepare_packaging dist/ssh_server_manager-X.Y.Z.tar.gz` and attach the
rendered formula to the release notes so a tap update is a copy, not a
reconstruction.
