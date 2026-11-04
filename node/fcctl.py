#!/usr/bin/env python3
"""Host-side helper: launch a Firecracker microVM with a vsock device and talk to sandboxd.

usage: fcctl.py <vmlinux> <rootfs.ext4> [--vcpu N] [--mem MiB]
Runs a demo session (ping, exec, write/read file, shutdown) and prints timings.
"""
import argparse, base64, json, os, shutil, socket, subprocess, sys, time, uuid

VSOCK_PORT = 5000


class Guest:
    """Client for sandboxd over Firecracker's vsock UDS (CONNECT <port> handshake)."""

    def __init__(self, uds):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(uds)
        self.s.sendall(f"CONNECT {VSOCK_PORT}\n".encode())
        self.f = self.s.makefile("rwb", buffering=0)
        ack = self.f.readline()
        if not ack.startswith(b"OK "):
            raise RuntimeError(f"vsock connect failed: {ack!r}")

    def call(self, op, **kw):
        req = {"id": uuid.uuid4().hex[:8], "op": op, **kw}
        self.f.write((json.dumps(req) + "\n").encode())
        line = self.f.readline()
        if not line:
            raise RuntimeError("guest closed connection")
        resp = json.loads(line)
        assert resp["id"] == req["id"]
        return resp

    def exec(self, cmd, **kw):
        return self.call("exec", cmd=cmd, **kw)


def wait_for_guest(uds, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            g = Guest(uds)
            g.call("ping")
            return g, time.monotonic() - t0
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    raise TimeoutError("sandboxd did not come up")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kernel"); ap.add_argument("rootfs")
    ap.add_argument("--vcpu", type=int, default=2); ap.add_argument("--mem", type=int, default=512)
    a = ap.parse_args()

    vm = f"vm-{uuid.uuid4().hex[:8]}"
    wd = f"/tmp/{vm}"; os.makedirs(wd)
    rootfs = f"{wd}/rootfs.ext4"; shutil.copyfile(a.rootfs, rootfs)  # per-VM writable copy
    api = f"{wd}/api.sock"; vsock = f"{wd}/v.sock"
    cfg = {
        "boot-source": {"kernel_image_path": a.kernel,
                        "boot_args": "console=ttyS0 reboot=k panic=1 pci=off quiet init=/sbin/fc-init"},
        "drives": [{"drive_id": "rootfs", "path_on_host": rootfs, "is_root_device": True, "is_read_only": False}],
        "machine-config": {"vcpu_count": a.vcpu, "mem_size_mib": a.mem},
        "vsock": {"guest_cid": 3, "uds_path": vsock},
    }
    json.dump(cfg, open(f"{wd}/config.json", "w"))
    log = open(f"{wd}/serial.log", "wb")
    t0 = time.monotonic()
    fc = subprocess.Popen(["firecracker", "--api-sock", api, "--config-file", f"{wd}/config.json"],
                          stdout=log, stderr=subprocess.STDOUT)
    try:
        g, t_ready = wait_for_guest(vsock)
        print(f"[{vm}] guest ready via vsock in {t_ready:.3f}s (firecracker pid {fc.pid})")

        r = g.exec("uname -r; id -u; nproc; hostname; cat /etc/os-release | head -1")
        print("exec#1:", json.dumps(r, ensure_ascii=False))

        payload = b"print('hello from inside the microVM')\n"
        print("write :", g.call("write_file", path="/root/hello.py", data_b64=base64.b64encode(payload).decode()))
        print("exec#2:", g.exec("python3 /root/hello.py")["stdout"].strip())
        rd = g.call("read_file", path="/root/hello.py")
        print("read  :", base64.b64decode(rd["data_b64"]) == payload, rd["bytes"], "bytes")

        print("exec#3 (timeout):", json.dumps(g.exec("sleep 5", timeout=1)))
        print("exec#4 (exit code):", g.exec("exit 7")["exit"])

        # exec latency: 20 round trips
        t = time.monotonic()
        for _ in range(20):
            g.exec("true")
        print(f"exec latency: {(time.monotonic() - t) / 20 * 1000:.1f} ms/round-trip")

        print("shutdown:", g.call("shutdown"))
        rc = fc.wait(timeout=10)
        print(f"firecracker exited rc={rc}; total session {time.monotonic() - t0:.2f}s")
    finally:
        if fc.poll() is None:
            fc.kill()
        log.close()
        tail = open(f"{wd}/serial.log", "rb").read().decode(errors="replace").splitlines()[-4:]
        print("serial tail:", *tail, sep="\n  ")


if __name__ == "__main__":
    main()
