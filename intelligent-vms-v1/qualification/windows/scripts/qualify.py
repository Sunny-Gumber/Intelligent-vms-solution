#!/usr/bin/env python3
"""Safe stdlib-only qualification evidence tooling."""
from __future__ import annotations
import argparse, csv, datetime as dt, json, os, re, subprocess
from pathlib import Path

STATES={"NOT_RUN","BLOCKED_EXTERNAL","PASS","FAIL","PASS_WITH_LIMITATION","NOT_APPLICABLE"}
SOURCES={"HOSTED_CI","VM_EXTERNAL","PHYSICAL_MACHINE","REAL_CAMERA","SIMULATED"}
EXTERNAL_ONLY_PREFIXES=("WIN10-","WIN11-","CAM-","PTZ-","EVENT-HW-","SOAK-","PERF-REAL-","STORAGE-REAL-")
RUN_RE=re.compile(r"^VMS-WIN-(\d{8})-(\d{3})$")
SECRET_PATTERNS=[
 re.compile(r"(?i)(password|secret|token|authorization|credential|private[_ -]?key)\s*[:=]\s*\S+"),
 re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+\-/]+=*"),
 re.compile(r"(?i)\b((?:rtsp|rtsps|http|https)://)[^/\s:@]+:[^@\s/]+@"),
]

def run_id(sequence:int, when:dt.date|None=None)->str:
    if not 1<=sequence<=999: raise ValueError("sequence must be 1..999")
    when=when or dt.datetime.now(dt.timezone.utc).date()
    return f"VMS-WIN-{when:%Y%m%d}-{sequence:03d}"

def load(path:str)->dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def redact(text:str)->str:
    out=text
    # Redact structured bearer/credential URLs before generic key=value patterns
    # so generic "authorization" matching cannot strand the bearer value.
    out=SECRET_PATTERNS[1].sub("Bearer [REDACTED]",out)
    out=SECRET_PATTERNS[2].sub(r"\1[REDACTED]@",out)
    out=SECRET_PATTERNS[0].sub(lambda m:f"{m.group(1)}=[REDACTED]",out)
    return out

def validate_result(d:dict)->list[str]:
    errors=[]
    if not RUN_RE.match(str(d.get("qualification_run_id",""))): errors.append("invalid qualification_run_id")
    b=d.get("build",{})
    if not re.fullmatch(r"[0-9a-f]{40}",str(b.get("git_sha",""))): errors.append("build.git_sha must be exact 40-char lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{64}",str(b.get("installer_sha256",""))): errors.append("build.installer_sha256 must be lowercase SHA-256")
    source=d.get("evidence_source")
    if source not in SOURCES: errors.append("invalid evidence_source")
    if d.get("overall_result","NOT_RUN") not in STATES: errors.append("invalid overall_result")
    for row in d.get("results",[]):
        tid=str(row.get("test_id","")); state=row.get("state"); rsource=row.get("evidence_source",source)
        if state not in STATES: errors.append(f"{tid}: invalid state")
        if rsource not in SOURCES: errors.append(f"{tid}: invalid evidence_source")
        if state in {"BLOCKED_EXTERNAL","NOT_APPLICABLE"} and not str(row.get("reason","")).strip():
            errors.append(f"{tid}: reason required for {state}")
        if state in {"PASS","PASS_WITH_LIMITATION"} and tid.startswith(EXTERNAL_ONLY_PREFIXES) and rsource in {"HOSTED_CI","SIMULATED"}:
            errors.append(f"{tid}: {rsource} cannot create real external PASS")
        if state=="FAIL":
            for key in ("expected","actual","severity"):
                if not str(row.get(key,"")).strip(): errors.append(f"{tid}: FAIL requires {key}")
    return errors

def summary(d:dict)->str:
    b=d.get("build",{})
    lines=["# Qualification summary - "+str(d.get("qualification_run_id","UNKNOWN")),"",
      "- Git SHA: "+str(b.get("git_sha","")),"- Installer: "+str(b.get("installer","")),
      "- Installer SHA-256: "+str(b.get("installer_sha256","")),"- Evidence source: "+str(d.get("evidence_source","")),
      "- Overall: "+str(d.get("overall_result","NOT_RUN")),"","| Test | State | Source | Reason |","|---|---|---|---|"]
    for r in d.get("results",[]):
        lines.append("| {} | {} | {} | {} |".format(r.get("test_id",""),r.get("state",""),r.get("evidence_source",d.get("evidence_source","")),str(r.get("reason","")).replace("|","/")))
    lines+=["","Evidence is bound to the exact Git SHA and installer SHA-256 above. Changed binaries require impact assessment or a new run."]
    return "\n".join(lines)+"\n"

def release_manifest(d:dict)->dict:
    b=d["build"]
    return {"product":"Intelligent VMS","product_version":b.get("product_version"),"git_sha":b.get("git_sha"),
      "installer":b.get("installer"),"installer_sha256":b.get("installer_sha256"),"server_version":b.get("server_version"),
      "client_version":b.get("client_version"),"db_schema_revision":b.get("db_schema_revision"),"mediamtx":b.get("mediamtx"),
      "installer_tool":"NSIS 3.13","qualification_run_id":d.get("qualification_run_id"),
      "qualification_status":d.get("overall_result"),"signing_status":d.get("signing_status","UNSIGNED_EXPECTED"),
      "known_limitations":d.get("known_limitations",[])}

def verify_signature(path:str, expect_unsigned:bool):
    if os.name!="nt": return ("UNSIGNED_EXPECTED" if expect_unsigned else "NOT_RUN","Authenticode verification requires Windows")
    escaped=str(Path(path)).replace("'","''")
    ps="(Get-AuthenticodeSignature -LiteralPath '"+escaped+"') | Select-Object Status,StatusMessage,SignerCertificate,TimeStamperCertificate | ConvertTo-Json -Depth 4"
    cp=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",ps],capture_output=True,text=True,timeout=30)
    if cp.returncode: return "FAIL",cp.stderr.strip()
    data=json.loads(cp.stdout); status=data.get("Status")
    if status=="Valid": return "SIGNED_VALID",json.dumps(data,sort_keys=True)
    if expect_unsigned and status in {"NotSigned","UnknownError"}: return "UNSIGNED_EXPECTED",json.dumps(data,sort_keys=True)
    return "FAIL",json.dumps(data,sort_keys=True)

def main():
    p=argparse.ArgumentParser(); sp=p.add_subparsers(dest="cmd",required=True)
    n=sp.add_parser("new-run"); n.add_argument("--sequence",type=int,required=True); n.add_argument("--git-sha",required=True); n.add_argument("--installer",required=True); n.add_argument("--installer-sha256",required=True); n.add_argument("--output",required=True)
    v=sp.add_parser("validate"); v.add_argument("--result",required=True)
    s=sp.add_parser("summarize"); s.add_argument("--result",required=True); s.add_argument("--output",required=True)
    r=sp.add_parser("release-manifest"); r.add_argument("--result",required=True); r.add_argument("--output",required=True)
    x=sp.add_parser("redact"); x.add_argument("--input",required=True); x.add_argument("--output",required=True)
    a=sp.add_parser("verify-signature"); a.add_argument("--artifact",required=True); a.add_argument("--expect-unsigned",action="store_true")
    perf=sp.add_parser("performance-template"); perf.add_argument("--output",required=True)
    args=p.parse_args()
    if args.cmd=="new-run":
        d={"qualification_run_id":run_id(args.sequence),"build":{"git_sha":args.git_sha,"installer":Path(args.installer).name,"installer_sha256":args.installer_sha256,"product_version":"0.2.0","server_version":"0.2.0","client_version":"0.2.0","db_schema_revision":"0018","mediamtx":{"version":"1.21.1","sha256":"faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"}},"evidence_source":"SIMULATED","overall_result":"NOT_RUN","signing_status":"UNSIGNED_EXPECTED","results":[],"known_limitations":["External Windows 10/11, camera, soak, performance and production signing evidence pending"]}
        Path(args.output).write_text(json.dumps(d,indent=2)+"\n",encoding="utf-8"); return 0
    if args.cmd=="validate":
        e=validate_result(load(args.result)); print("\n".join(e) if e else "qualification_result_valid"); return 1 if e else 0
    if args.cmd=="summarize": Path(args.output).write_text(summary(load(args.result)),encoding="utf-8"); return 0
    if args.cmd=="release-manifest": Path(args.output).write_text(json.dumps(release_manifest(load(args.result)),indent=2)+"\n",encoding="utf-8"); return 0
    if args.cmd=="redact": Path(args.output).write_text(redact(Path(args.input).read_text(encoding="utf-8")),encoding="utf-8"); return 0
    if args.cmd=="verify-signature":
        st,msg=verify_signature(args.artifact,args.expect_unsigned); print(json.dumps({"status":st,"detail":msg})); return 0 if st in {"SIGNED_VALID","UNSIGNED_EXPECTED","NOT_RUN"} else 1
    if args.cmd=="performance-template":
        with Path(args.output).open("w",newline="",encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp_utc","scenario","active_cameras","live_streams","recording_cameras","cpu_percent","ram_percent","working_set_mb","gpu_percent","network_rx_mbps","network_tx_mbps","disk_write_mbps","error_count"])
        return 0
    return 2
if __name__=="__main__": raise SystemExit(main())
