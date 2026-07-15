# Troubleshooting playbook

## Local UI does not open

Run the default launcher first; it selects a free loopback port and opens the
browser:

```bash
./scripts/serverctl ui
```

If a fixed port is required and it is occupied, either stop the process that
owns it or use automatic selection:

```bash
./scripts/serverctl ui --port 0
```

For a manually opened browser, keep the one-time URL in a private file rather
than printing it:

```bash
./scripts/serverctl ui --no-open --url-file /tmp/ssh-server-manager-url
```

The file is mode `600` and is intended for the same local user only.

## Remote command reports “No such file or directory”

`serverctl exec` treats arguments literally. A full command string such as
`printf ...; uname -a` becomes one executable name unless shell mode is explicit:

```bash
./scripts/serverctl exec ALIAS --shell -- 'printf "host=%s\\n" "$(hostname)"; uname -a'
```

For a normal executable, pass the executable and arguments separately:

```bash
./scripts/serverctl exec ALIAS -- uname -a
```

## Sending a file or script

For a text script, use:

```bash
cat local-script.sh | ./scripts/serverctl exec ALIAS --stdin -- sh -s
```

For a binary artifact, stream raw bytes directly:

```bash
cat artifact.tar.gz | ./scripts/serverctl exec ALIAS --stdin-binary -- sh -c 'cat > artifact.tar.gz'
```

Keep streamed data out of logs and do not use this path for credentials.

## SSH succeeds but a web port is unreachable

First verify the service on the server itself, then verify the container or
process is bound to the intended interface. If localhost works but the public
address times out, the cloud security group or upstream firewall needs to allow
that TCP port; changing OpenSSH settings will not fix it.
