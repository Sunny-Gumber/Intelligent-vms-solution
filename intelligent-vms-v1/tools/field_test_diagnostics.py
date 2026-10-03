#!/usr/bin/env python3
"""Collect a bounded secret-redacted Ubuntu field-test diagnostics archive."""
from __future__ import annotations
import argparse, json, os, re, subprocess, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; SENSITIVE=("SECRET","TOKEN","PASSWORD","PRIVATE_KEY")
def env_values(path:Path):
    """Read non-comment environment assignments without evaluating shell content."""
    out={}
    if not path.is_file(): return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line=raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k,v=line.split("=",1); out[k.strip()]=v.strip()
    return out
def run(args,timeout=15):
    """Run one bounded diagnostic command and return capped combined output."""
    try:
        r=subprocess.run(args,cwd=ROOT,check=False,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout,env={**os.environ,"LC_ALL":"C"})
        return r.stdout[-200000:]
    except (OSError,subprocess.TimeoutExpired) as exc: return f"command unavailable: {type(exc).__name__}\n"
def redactor(values):
    """Build a text redactor from configured sensitive environment values."""
    secrets=sorted({v for k,v in values.items() if v and any(x in k.upper() for x in SENSITIVE)},key=len,reverse=True)
    def redact(text):
        for value in secrets: text=text.replace(value,"[REDACTED]")
        text=re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+\-/]+=*","Bearer [REDACTED]",text)
        text=re.sub(r"(?i)(token|password)=([^\s&]+)",r"\1=[REDACTED]",text)
        text=re.sub(r"(?i)\b((?:rtsp|rtsps|http|https)://)[^/\s:@]+:[^@\s/]+@",r"\1[REDACTED]@",text)
        return re.sub(r"(postgresql(?:\+\w+)?://[^:\s/]+:)[^@\s]+@",r"\1[REDACTED]@",text)
    return redact
def collect(env_file:Path,output:Path|None=None):
    """Collect redacted field-test evidence into a mode-0600 tar.gz archive."""
    values=env_values(env_file); redact=redactor(values); stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); output=output or ROOT/f"field-test-diagnostics-{stamp}.tar.gz"; recording=values.get("VMS_RECORDING_VOLUME","")
    commands={"os-release.txt":["cat","/etc/os-release"],"uname.txt":["uname","-a"],"docker-version.txt":["docker","version"],"compose-version.txt":["docker","compose","version"],"compose-ps.txt":["docker","compose","--env-file",str(env_file),"ps"],"compose-images.txt":["docker","compose","--env-file",str(env_file),"images"],"api-readiness.txt":["curl","-fsS","http://127.0.0.1:8000/api/v1/system/healthz/ready"],"vms-health.txt":["curl","-fsS","http://127.0.0.1:8000/api/v1/system/health"],"mediamtx-metrics.txt":["curl","-fsS","--max-time","3","http://127.0.0.1:9998/metrics"],"alembic-current.txt":["docker","compose","--env-file",str(env_file),"run","--rm","--no-deps","control-api","alembic","current"]}
    if recording: commands["recording-disk.txt"]=["df","-h",recording]; commands["recording-path.txt"]=["stat","-c","%A %U:%G %n",recording]
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); presence={k:bool(values.get(k,"")) for k in ("DATABASE_URL","VMS_SECRET_KEY","LIVE_VIEW_TOKEN_PRIVATE_KEY_B64","AUTH_DISABLED","AUTH_REQUIRE_OIDC","AUTH_BROWSER_SESSION_ENABLED","RECORDING_HOOK_TOKEN","VMS_RECORDING_VOLUME","ONVIF_SITE_ALLOWED_CIDRS_JSON")}
        (root/"config-presence.json").write_text(json.dumps(presence,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        for name,args in commands.items(): (root/name).write_text(redact(run(args)),encoding="utf-8")
        logs=run(["docker","compose","--env-file",str(env_file),"logs","--no-color","--tail","500","control-api","mediamtx","web","postgres","clickhouse"],30); (root/"recent-logs.txt").write_text(redact(logs),encoding="utf-8")
        with tarfile.open(output,"w:gz") as archive:
            for item in sorted(root.iterdir()): archive.add(item,arcname=item.name)
    os.chmod(output,0o600); print(f"field_test_diagnostics_ok file={output}"); return output
def main():
    """Parse CLI arguments and collect one diagnostics archive."""
    p=argparse.ArgumentParser(); p.add_argument("--env-file",type=Path,default=ROOT/".env"); p.add_argument("--output",type=Path); a=p.parse_args(); collect(a.env_file,a.output)
if __name__=="__main__": main()
