#!/usr/bin/env python3
"""Generate a private, non-production Ubuntu field-test environment file."""
from __future__ import annotations
import argparse, base64, os, secrets, stat, subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]; TEMPLATE=ROOT/".env.example"; OUTPUT=ROOT/".env"

def _secret(): return secrets.token_hex(32)
def _rsa():
    r=subprocess.run(["openssl","genpkey","-algorithm","RSA","-pkeyopt","rsa_keygen_bits:2048"],check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    return base64.b64encode(r.stdout).decode("ascii")
def _replace(lines,values):
    found=set(); out=[]
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key=line.split("=",1)[0].strip()
            if key in values: out.append(f"{key}={values[key]}"); found.add(key); continue
        out.append(line)
    out.extend(f"{k}={v}" for k,v in values.items() if k not in found)
    return out

_TEMP_NAME_NONCE_BYTES=8

def _write_all_bytes(fd:int, payload:bytes) -> None:
    """Write every payload byte to fd, retrying when a signal interrupts the write."""
    view=memoryview(payload)
    while view:
        try:
            written=os.write(fd, view)
        except InterruptedError:
            continue
        if written<=0:
            raise OSError("incomplete private write")
        view=view[written:]

def _remove_private_temp(path:Path) -> None:
    """Remove a mode-0600 temp file after a failed publish. Missing files are already gone."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return

def _atomic_write_private(destination:Path, text:str) -> None:
    """Publish text via a same-directory mode-0600 temp file, then atomic replace.

    os.open uses an explicit 0600 mode and fchmod repeats it before any payload byte
    is written, so umask 022 cannot create a group-readable secret file. Failure
    before os.replace removes the temp file and leaves destination unchanged.
    """
    mode=stat.S_IRUSR|stat.S_IWUSR
    temporary=destination.parent/f"{destination.name}.{secrets.token_hex(_TEMP_NAME_NONCE_BYTES)}.part"
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags|=os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags|=os.O_NOFOLLOW
    fd=os.open(temporary, flags, mode)
    try:
        os.fchmod(fd, mode)
        _write_all_bytes(fd, text.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            # Re-raise the original failure. A second close error must not replace it.
            pass
        _remove_private_temp(temporary)
        raise
    try:
        os.replace(temporary, destination)
    except Exception:
        _remove_private_temp(temporary)
        raise

def generate(recordings_dir:Path,camera_cidrs:list[str],force=False):
    """Write the field-test env file with owner-only permissions before any secret bytes."""
    recordings_dir=recordings_dir.expanduser().resolve()
    if recordings_dir==Path("/"): raise ValueError("recording directory must not be filesystem root")
    if OUTPUT.exists() and not force: raise FileExistsError(f"{OUTPUT} already exists; refuse to overwrite")
    recordings_dir.mkdir(parents=True,exist_ok=True); os.chmod(recordings_dir,0o750)
    if not os.access(recordings_dir,os.W_OK): raise PermissionError(f"recording directory is not writable: {recordings_dir}")
    pg=_secret(); cidrs=list(camera_cidrs) or ["192.168.0.0/16","127.0.0.1/32"]
    if "127.0.0.1/32" not in cidrs: cidrs.append("127.0.0.1/32")
    values={
      "POSTGRES_PASSWORD":pg,"DATABASE_URL":f"postgresql+asyncpg://vms:{pg}@postgres:5432/vms",
      "VMS_SECRET_KEY":_secret(),"LIVE_VIEW_TOKEN_PRIVATE_KEY_B64":_rsa(),"LIVE_VIEW_TOKEN_VERIFICATION_PUBLIC_KEYS_B64_JSON":"[]",
      "AUTO_CREATE_SCHEMA":"false","AUTH_DISABLED":"false","AUTH_REQUIRE_OIDC":"false","AUTH_HS256_SECRET":_secret(),
      "AUTH_BROWSER_SESSION_ENABLED":"true","AUTH_BROWSER_SESSION_COOKIE_SECURE":"false","AUTH_BROWSER_SESSION_MAX_AGE_SECONDS":"28800",
      "CORS_ALLOWED_ORIGINS":"http://localhost:8080","CORS_ALLOWED_HEADERS":"Authorization,Content-Type,Range,X-VMS-CSRF",
      "RECORDING_HOOK_TOKEN":_secret(),"REGIONAL_SPOOL_TOKEN":_secret(),"NODE_AGENT_TOKEN":_secret(),
      "VMS_RESTART_POLICY":"unless-stopped","VMS_BIND_ADDRESS":"127.0.0.1","VMS_ADMIN_BIND_ADDRESS":"127.0.0.1",
      "VMS_WEBRTC_UDP_BIND_ADDRESS":"0.0.0.0","VMS_RECORDING_VOLUME":str(recordings_dir),
      "MEDIAMTX_WEBRTC_PUBLIC_BASE":"http://localhost:8889","MEDIAMTX_HLS_PUBLIC_BASE":"http://localhost:8888",
      "ONVIF_SITE_ALLOWED_CIDRS_JSON":'{"field-test/site-01":['+','.join(f'"{x}"' for x in cidrs)+"]}",
      "ONVIF_DISCOVERY_LOCAL_SITES":"field-test/site-01","COMPOSE_PROJECT_NAME":"intelligent-vms-field-test",
    }
    rendered="\n".join(_replace(TEMPLATE.read_text(encoding="utf-8").splitlines(),values))+"\n"
    _atomic_write_private(OUTPUT, rendered)
    os.chmod(OUTPUT,stat.S_IRUSR|stat.S_IWUSR)
    print(f"field_test_env_ok file={OUTPUT} recordings={recordings_dir}")
    print("secret values were generated but intentionally not printed")
    return OUTPUT
def main():
    p=argparse.ArgumentParser(); p.add_argument("--recordings-dir",required=True,type=Path); p.add_argument("--camera-cidr",action="append",default=[]); p.add_argument("--force",action="store_true")
    a=p.parse_args(); generate(a.recordings_dir,a.camera_cidr,a.force)
if __name__=="__main__": main()
