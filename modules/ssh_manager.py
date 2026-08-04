"""
SSH Manager — handles key listing, generation, key-trust push, test connections,
and interactive shell sessions (used by the WebSocket terminal).
"""

import os
import glob
import threading
import subprocess
import time
import logging
from typing import Dict, List, Optional
from pathlib import Path
import paramiko

logger = logging.getLogger(__name__)

SSH_DIR = Path.home() / ".ssh"


class SSHManager:
    def __init__(self):
        self._shells: dict[str, dict] = {}   # sid -> {channel, client}

    # ──────────────────────────────────────────────────────
    # Key utilities
    # ──────────────────────────────────────────────────────

    def list_local_keys(self) -> List[Dict]:
        """Return private keys found in ~/.ssh/."""
        keys = []
        if not SSH_DIR.exists():
            return keys
        pub_files = {p.stem for p in SSH_DIR.glob("*.pub")}
        for p in SSH_DIR.iterdir():
            if p.suffix == ".pub" or p.name.startswith("known_hosts") \
                    or p.name.startswith("authorized") \
                    or p.name in ("config", "environment"):
                continue
            if p.stem in pub_files and p.is_file():
                pub_path = SSH_DIR / (p.name + ".pub")
                pub_content = ""
                try:
                    pub_content = pub_path.read_text().strip()
                except Exception:
                    pass
                keys.append({
                    "name": p.name,
                    "private_key_path": str(p),
                    "public_key_path": str(pub_path),
                    "public_key": pub_content,
                })
        return keys

    def generate_key(self, key_type="ed25519", key_name="id_powerpilot",
                     passphrase="", comment="powerpilot") -> dict:
        SSH_DIR.mkdir(mode=0o700, exist_ok=True)
        key_path = SSH_DIR / key_name
        if key_path.exists():
            return {"error": f"Key '{key_name}' already exists at {key_path}"}
        cmd = [
            "ssh-keygen", "-t", key_type, "-f", str(key_path),
            "-C", comment, "-N", passphrase,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            pub = (key_path.with_suffix("") if key_type != "ed25519"
                   else Path(str(key_path) + ".pub"))
            pub_path = SSH_DIR / (key_name + ".pub")
            return {
                "ok": True,
                "private_key_path": str(key_path),
                "public_key_path": str(pub_path),
                "public_key": pub_path.read_text().strip() if pub_path.exists() else "",
            }
        except Exception as exc:
            return {"error": str(exc)}

    # ──────────────────────────────────────────────────────
    # SSH helpers
    # ──────────────────────────────────────────────────────

    def _connect(self, host, port, username, key_path, password):
        """Return a connected SSHClient."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, port=port, username=username,
                  timeout=10, banner_timeout=10)
        if key_path:
            kw["key_filename"] = os.path.expanduser(key_path)
        elif password:
            kw["password"] = password
            kw["look_for_keys"] = False
        else:
            kw["look_for_keys"] = True
        client.connect(**kw)
        return client

    def run_hmc_command(self, host, port=22, username="hscroot",
                        key_path=None, password=None,
                        command="", stdin_input=None) -> dict:
        """Run a single command over its own SSH connection.

        stdin_input: optional string to write to the command's stdin.
                     For interactive prompts (e.g. Brocade cfgsave asking 'yes/no'),
                     we wait briefly for the prompt to appear then send the answer.
        """
        try:
            client = self._connect(host, port, username, key_path, password)
            stdin, stdout, stderr = client.exec_command(command, timeout=60)

            if stdin_input is not None:
                # Give the remote command time to print its prompt before we answer.
                # select.select() does not work on Windows for non-socket fds,
                # so we use a simple polling loop with recv_ready().
                answer = stdin_input if stdin_input.endswith("\n") else stdin_input + "\n"
                deadline = time.time() + 15
                while time.time() < deadline:
                    if stdout.channel.recv_ready():
                        break
                    if stdout.channel.exit_status_ready():
                        break
                    time.sleep(0.2)
                stdin.write(answer)
                stdin.flush()
                stdin.channel.shutdown_write()

            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_status = stdout.channel.recv_exit_status()
            client.close()
            if exit_status != 0 and not out:
                return {"ok": False, "error": err or f"exit {exit_status}"}
            return {"ok": True, "output": out, "stderr": err}
        except paramiko.AuthenticationException:
            return {"ok": False, "error": "SSH authentication failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_hmc_commands_batch(self, host, port=22, username="hscroot",
                               key_path=None, password=None,
                               commands: dict = None) -> dict:
        """Run multiple commands over a SINGLE SSH connection.

        ``commands`` is an ordered dict of  {key: cmd_string_or_(cmd, stdin)}.
        Returns {key: {"ok": bool, "output": str, "error": str}}.
        Opens one connection, executes each command sequentially,
        closes the connection once at the end.
        """
        results = {k: {"ok": False, "output": "", "error": "not run"}
                   for k in (commands or {})}
        if not commands:
            return results
        try:
            client = self._connect(host, port, username, key_path, password)
        except paramiko.AuthenticationException:
            err = "SSH authentication failed"
            return {k: {"ok": False, "output": "", "error": err} for k in commands}
        except Exception as exc:
            err = str(exc)
            return {k: {"ok": False, "output": "", "error": err} for k in commands}

        for key, spec in commands.items():
            # Support (cmd, stdin_input) tuples
            if isinstance(spec, tuple):
                cmd, stdin_val = spec
            else:
                cmd, stdin_val = spec, None
            try:
                stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
                if stdin_val is not None:
                    answer = stdin_val if stdin_val.endswith("\n") else stdin_val + "\n"
                    # Poll briefly for prompt before answering
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        if stdout.channel.recv_ready() or stdout.channel.exit_status_ready():
                            break
                        time.sleep(0.2)
                    stdin.write(answer)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                out = stdout.read().decode("utf-8", errors="replace").strip()
                err = stderr.read().decode("utf-8", errors="replace").strip()
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0 and not out:
                    results[key] = {"ok": False, "output": "", "error": err or f"exit {exit_status}"}
                else:
                    results[key] = {"ok": True, "output": out, "stderr": err}
            except Exception as exc:
                results[key] = {"ok": False, "output": "", "error": str(exc)}

        try:
            client.close()
        except Exception:
            pass
        return results

    # ──────────────────────────────────────────────────────
    # LPAR creation
    # ──────────────────────────────────────────────────────

    def create_lpar(self, host, port=22, username="hscroot",
                    key_path=None, managed_system="", params=None) -> dict:
        """Build and run mksyscfg -r lpar on the HMC via SSH.

        Returns {"ok": True, "output": ..., "command": ...} on success,
        or {"ok": False, "error": ..., "command": ...} on failure.
        """
        p = params or {}

        # ── Core identity ─────────────────────────────────
        parts = [
            f'name={p["name"]}',
            f'lpar_id={p["lpar_id"]}',
            f'lpar_env={p.get("lpar_env","aixlinux")}',
            f'profile_name={p.get("profile_name","default_profile")}',
            f'boot_mode={p.get("boot_mode","norm")}',
            f'lpar_proc_compat_mode={p.get("lpar_proc_compat_mode","default")}',
            f'max_virtual_slots={p.get("max_virtual_slots","100")}',
            f'conn_monitoring={p.get("conn_monitoring","1")}',
            f'sync_curr_profile={p.get("sync_curr_profile","1")}',
            f'allow_perf_collection={p.get("allow_perf_collection","1")}',
        ]

        # ── Memory (GB → MB conversion) ───────────────────
        def gb_to_mb(val):
            try:
                return str(int(float(val)) * 1024)
            except (TypeError, ValueError):
                return str(val)

        parts += [
            f'min_mem={gb_to_mb(p.get("min_mem"))}',
            f'desired_mem={gb_to_mb(p.get("desired_mem"))}',
            f'max_mem={gb_to_mb(p.get("max_mem"))}',
        ]

        # ── Processor ─────────────────────────────────────
        parts += [
            f'proc_mode={p.get("proc_mode","shared")}',
            f'min_proc_units={p.get("min_proc_units","0.1")}',
            f'desired_proc_units={p.get("desired_proc_units","0.5")}',
            f'max_proc_units={p.get("max_proc_units","1")}',
            f'min_procs={p.get("min_procs","1")}',
            f'desired_procs={p.get("desired_procs","1")}',
            f'max_procs={p.get("max_procs","1")}',
            f'sharing_mode={p.get("sharing_mode","uncap")}',
        ]
        if p.get("proc_mode", "shared") == "shared":
            if p.get("uncap_weight"):
                parts.append(f'uncap_weight={p["uncap_weight"]}')

        # ── Virtual adapters ──────────────────────────────
        # vETH / vSCSI: virtual_eth_adapters="spec1","spec2"
        # vFC:          \"virtual_fc_adapters=\"\"spec1\"\",\"\"spec2\"\"\"
        BQ   = '\\"'           # one backslash-quote:  \"
        BQBQ = '\\"' + '\\"'   # two backslash-quotes: \"\"

        if p.get("virtual_eth_adapters"):
            eth = ','.join('"' + s + '"' for s in p["virtual_eth_adapters"])
            parts.append('virtual_eth_adapters=' + eth)
        if p.get("virtual_fc_adapters"):
            fc_specs = (BQBQ + ',' + BQBQ).join(p["virtual_fc_adapters"])
            parts.append(BQ + 'virtual_fc_adapters=' + BQBQ + fc_specs + BQBQ + BQ)
        if p.get("virtual_scsi_adapters"):
            scsi = ','.join('"' + s + '"' for s in p["virtual_scsi_adapters"])
            parts.append('virtual_scsi_adapters=' + scsi)

        param_str = ",".join(parts)
        command = f'mksyscfg -r lpar -m "{managed_system}" -i "{param_str}"'

        logger.info("create_lpar command: %s", command)
        result = self.run_hmc_command(
            host=host, port=port, username=username,
            key_path=key_path, command=command,
        )
        result["command"] = command

        # ── Interpret mksyscfg result ─────────────────────
        # mksyscfg exits 0 on success (stdout empty).
        # Failures may arrive as: exit != 0, or exit 0 with an error
        # message on stdout/stderr containing "error" (case-insensitive).
        if result.get("ok"):
            output = result.get("output", "")
            stderr = result.get("stderr", "")
            combined = (output + " " + stderr).lower()
            if "error" in combined:
                # Pull the most informative text available
                error_text = output or stderr
                result["ok"]    = False
                result["error"] = error_text.strip()
            else:
                result["message"] = output or "LPAR created successfully."
        return result

    # ──────────────────────────────────────────────────────
    # Connection testing
    # ──────────────────────────────────────────────────────

    def test_connection(self, host, port=22, username="hscroot",
                        key_path=None, password=None) -> dict:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs = dict(hostname=host, port=port, username=username,
                                  timeout=10, banner_timeout=10)
            if key_path:
                connect_kwargs["key_filename"] = os.path.expanduser(key_path)
            elif password:
                connect_kwargs["password"] = password
                connect_kwargs["look_for_keys"] = False
            else:
                connect_kwargs["look_for_keys"] = True
            client.connect(**connect_kwargs)
            _, stdout, _ = client.exec_command("lshmc -V 2>/dev/null || uname -a")
            version = stdout.read().decode().strip()
            client.close()
            return {"ok": True, "version": version}
        except paramiko.AuthenticationException:
            return {"ok": False, "error": "Authentication failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ──────────────────────────────────────────────────────
    # Key trust (ssh-copy-id equivalent)
    # ──────────────────────────────────────────────────────

    def push_public_key(self, host, port=22, username="hscroot",
                        password="", public_key_path="~/.ssh/id_rsa.pub") -> dict:
        pub_path = Path(os.path.expanduser(public_key_path))
        if not pub_path.exists():
            return {"error": f"Public key not found: {pub_path}"}
        pub_key = pub_path.read_text().strip()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(hostname=host, port=port, username=username,
                           password=password, look_for_keys=False, timeout=15)
            # HMC restricted shell only supports mkauthkeys.
            # Syntax: mkauthkeys --add [-u user-ID] "string"
            # The key string MUST be the last argument.
            cmd = f'mkauthkeys --add -u {username} "{pub_key}"'
            _, stdout, stderr = client.exec_command(cmd)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            exit_status = stdout.channel.recv_exit_status()
            client.close()
            if exit_status != 0:
                return {"ok": False, "error": err or f"mkauthkeys exited {exit_status}"}
            return {"ok": True, "message": "Public key installed on HMC via mkauthkeys"}
        except paramiko.AuthenticationException:
            return {"ok": False, "error": "Authentication failed — check password"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ──────────────────────────────────────────────────────
    # Interactive shell
    # ──────────────────────────────────────────────────────

    def open_shell(self, sid, host, port=22, username="hscroot",
                   key_path=None, password=None, emit_fn=None) -> dict:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs = dict(hostname=host, port=port, username=username,
                                  timeout=15)
            if key_path:
                connect_kwargs["key_filename"] = os.path.expanduser(key_path)
            elif password:
                connect_kwargs["password"] = password
                connect_kwargs["look_for_keys"] = False
            else:
                connect_kwargs["look_for_keys"] = True
            client.connect(**connect_kwargs)
            channel = client.invoke_shell(term="xterm", width=220, height=50)
            self._shells[sid] = {"client": client, "channel": channel}
            # Read loop in background thread
            t = threading.Thread(target=self._read_loop,
                                 args=(sid, channel, emit_fn), daemon=True)
            t.start()
            return {"ok": True}
        except paramiko.AuthenticationException:
            return {"ok": False, "error": "Authentication failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _read_loop(self, sid, channel, emit_fn):
        try:
            while not channel.closed:
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    if emit_fn:
                        emit_fn(data)
                elif channel.exit_status_ready():
                    break
                else:
                    time.sleep(0.02)
        except Exception as exc:
            logger.debug("SSH read loop ended for %s: %s", sid, exc)
        finally:
            if emit_fn:
                emit_fn("\r\n[Connection closed]\r\n")

    def send_input(self, sid, data: str):
        shell = self._shells.get(sid)
        if shell and not shell["channel"].closed:
            shell["channel"].send(data)

    def close_shell(self, sid):
        shell = self._shells.pop(sid, None)
        if shell:
            try:
                shell["channel"].close()
                shell["client"].close()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────
    # LPAR vterm console (mkvterm)
    # ──────────────────────────────────────────────────────

    def open_vterm(self, sid, host, port=22, username="hscroot",
                   key_path=None, password=None,
                   managed_system="", lpar_name="",
                   emit_fn=None) -> dict:
        """Open an SSH connection to the HMC and run mkvterm for a specific LPAR.

        The mkvterm command opens a virtual terminal (vterm) session directly to
        the LPAR's console.  We request a PTY so that the HMC allocates a
        pseudo-terminal, then exec the command (not invoke_shell) so that when
        the channel closes the HMC automatically frees the vterm lock.

        Parameters
        ----------
        sid            : unique session id (Flask-SocketIO session id)
        emit_fn        : callable(data:str) – called with raw terminal bytes
        managed_system : HMC managed system name (e.g. "Server-9080-M9S-XXXXXXX")
        lpar_name      : LPAR name (e.g. "AIX01")
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs = dict(
                hostname=host, port=port, username=username, timeout=15
            )
            if key_path:
                connect_kwargs["key_filename"] = os.path.expanduser(key_path)
            elif password:
                connect_kwargs["password"] = password
                connect_kwargs["look_for_keys"] = False
            else:
                connect_kwargs["look_for_keys"] = True
            client.connect(**connect_kwargs)

            # Open a transport channel with a PTY so the HMC console is
            # interactive (handles control sequences, resize, etc.).
            transport = client.get_transport()
            channel = transport.open_session()
            channel.get_pty(term="xterm-256color", width=220, height=50)

            cmd = f'mkvterm -m "{managed_system}" -p "{lpar_name}"'
            channel.exec_command(cmd)

            self._shells[sid] = {"client": client, "channel": channel,
                                 "type": "vterm"}

            # Start the background read loop
            t = threading.Thread(
                target=self._read_loop,
                args=(sid, channel, emit_fn),
                daemon=True,
            )
            t.start()
            return {"ok": True, "command": cmd}
        except paramiko.AuthenticationException:
            try:
                client.close()
            except Exception:
                pass
            return {"ok": False, "error": "SSH authentication failed"}
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            return {"ok": False, "error": str(exc)}

    def resize_vterm(self, sid, cols: int, rows: int):
        """Send a terminal resize (SIGWINCH) to the SSH channel."""
        shell = self._shells.get(sid)
        if shell and not shell["channel"].closed:
            try:
                shell["channel"].resize_pty(width=cols, height=rows)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────
    # Script deployment (SFTP / SCP)
    # ──────────────────────────────────────────────────────

    def deploy_file(self, local_path, host, port=22, username="root",
                    password="", target_path="", method="sftp") -> dict:
        """Upload a local file to a remote server via SFTP or SCP.

        Parameters
        ----------
        local_path  : absolute path of the local file to upload
        host        : remote host / IP
        port        : SSH port (default 22)
        username    : remote username
        password    : remote password
        target_path : remote destination. May be a directory (ends with '/')
                      or a full file path. If a directory, the local filename
                      is appended.
        method      : "sftp" or "scp" (both run over the SSH transport)

        Returns {"ok": True, "message": ...} or {"ok": False, "error": ...}.
        """
        local = Path(os.path.expanduser(local_path))
        if not local.is_file():
            return {"ok": False, "error": f"Local file not found: {local}"}

        filename = local.name

        # Resolve the remote destination path.
        dest = (target_path or "").strip()
        if not dest:
            dest = filename
        elif dest.endswith("/"):
            dest = dest + filename

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(hostname=host, port=port, username=username,
                           password=password, look_for_keys=False,
                           allow_agent=False, timeout=15, banner_timeout=15)
        except paramiko.AuthenticationException:
            return {"ok": False, "error": "Authentication failed — check username/password"}
        except Exception as exc:
            return {"ok": False, "error": f"Connection failed: {exc}"}

        try:
            if method == "scp":
                return self._deploy_scp(client, str(local), dest, filename)
            return self._deploy_sftp(client, str(local), dest, filename)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _deploy_sftp(self, client, local, dest, filename) -> dict:
        """Upload via SFTP subsystem.

        The "mode out of range" error is raised by paramiko when it packs an
        out-of-range permission mode into an SFTP attribute message. This can
        happen in three places:

          1. ``sftp.put`` — it reads the *local* file's ``os.stat().st_mode``
             (on Windows this can overflow the 32-bit SFTP field).
          2. ``sftp.stat``/directory-detection — some restricted SFTP servers
             (e.g. VIOS ``padmin``) return unexpected mode values.
          3. ``sftp.chmod`` with a bad mode value.

        To be robust we:
          * Detect a directory target *without* trusting the returned mode
            (we simply try ``listdir`` on the path).
          * Upload with ``putfo`` (streamed, no local-mode preservation).
          * chmod with a plain, in-range octal literal, and never let a chmod
            failure abort an otherwise-successful upload.
        """
        try:
            sftp = client.open_sftp()
            try:
                # If the caller pointed at an existing directory, append the
                # local filename. Use listdir() rather than stat().st_mode so we
                # never touch a possibly-out-of-range mode value.
                if not dest.endswith("/" + filename):
                    try:
                        sftp.listdir(dest)      # succeeds only for directories
                        dest = dest.rstrip("/") + "/" + filename
                    except IOError:
                        # Not a directory (or does not exist) — treat as a file
                        # path. That's fine for a new upload.
                        pass

                file_size = os.path.getsize(local)
                with open(local, "rb") as fh:
                    # confirm=False avoids a post-upload stat() round-trip and
                    # putfo does not preserve the local file's mode bits, so the
                    # "mode out of range" condition cannot be triggered here.
                    sftp.putfo(fh, dest, file_size=file_size, confirm=False)

                # Best-effort permission set. A plain octal literal is always in
                # range; wrap in try/except so a restricted SFTP server that
                # rejects chmod does not fail the whole deployment.
                try:
                    sftp.chmod(dest, 0o755)
                except Exception as chmod_exc:
                    logger.debug("SFTP chmod skipped for %s: %s", dest, chmod_exc)
            finally:
                sftp.close()
            return {"ok": True, "message": f"Deployed via SFTP to {dest}",
                    "target": dest}
        except Exception as exc:
            return {"ok": False, "error": f"SFTP upload failed: {exc}"}

    def _deploy_scp(self, client, local, dest, filename) -> dict:
        """Upload via SCP protocol (scp -t on the remote host)."""
        try:
            file_size = os.path.getsize(local)
            transport = client.get_transport()
            channel = transport.open_session()
            channel.settimeout(30)
            # scp -t writes an incoming file to <dest>. If dest is a directory
            # the remote scp appends the filename automatically.
            channel.exec_command(f'scp -t "{dest}"')

            def _wait_ok():
                resp = channel.recv(1)
                if resp != b"\x00":
                    extra = b""
                    while channel.recv_ready():
                        extra += channel.recv(1024)
                    raise IOError((resp + extra).decode(errors="replace").strip()
                                  or "SCP protocol error")

            _wait_ok()
            channel.sendall(f"C0755 {file_size} {filename}\n".encode())
            _wait_ok()
            with open(local, "rb") as fh:
                while True:
                    chunk = fh.read(32768)
                    if not chunk:
                        break
                    channel.sendall(chunk)
            channel.sendall(b"\x00")
            _wait_ok()
            channel.close()
            return {"ok": True, "message": f"Deployed via SCP to {dest}",
                    "target": dest}
        except Exception as exc:
            return {"ok": False, "error": f"SCP upload failed: {exc}"}


