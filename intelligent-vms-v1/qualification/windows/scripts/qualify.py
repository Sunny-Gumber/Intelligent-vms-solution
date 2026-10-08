#!/usr/bin/env python3
"""Safe stdlib-only qualification evidence tooling."""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, json, os, re, struct, subprocess, sys
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

PRODUCT_MANIFEST=Path("release/windows/product-version.json")
RUNTIME_MANIFEST=Path("infra/mediamtx/runtime.json")
SHA256_NOT_PROVIDED="NOT_PROVIDED"
SHA256_COMPUTED="COMPUTED"
PRODUCT_FIELDS=(
 ("product","product"),
 ("product_version","product_version"),
 ("installer_version","installer_version"),
 ("server_version","server_package_version"),
 ("client_version","client_package_version"),
 ("db_schema_revision","schema_baseline"),
)
SUPPLIED_IDENTITY_FIELDS=(
 ("product_version","product_version"),
 ("installer_version","installer_version"),
 ("server_version","server_version"),
 ("client_version","client_version"),
 ("schema_revision","db_schema_revision"),
 ("mediamtx_version","mediamtx_version"),
)
RUNTIME_PINS=("windows_zip_sha256","windows_exe_sha256")

class QualificationIdentityError(ValueError):
    """Canonical qualification identity could not be loaded or did not match."""

def product_root_from(explicit:str|None)->Path:
    """Resolve the product root that holds the canonical identity manifests.

    Args:
        explicit: Caller-supplied product root, or None for the repository root
            that contains this script.

    Returns:
        Directory containing release/windows/product-version.json and
        infra/mediamtx/runtime.json.

    Raises:
        QualificationIdentityError: The supplied root is blank or is not a directory.
    """
    if explicit is not None:
        if not explicit.strip():
            raise QualificationIdentityError("product root missing: blank path")
        root=Path(explicit)
        if not root.is_dir():
            raise QualificationIdentityError(f"product root missing: {root}")
        return root
    return Path(__file__).resolve().parents[3]

def read_manifest(path:Path)->dict:
    """Load one canonical JSON manifest.

    Args:
        path: Manifest file to read.

    Returns:
        The manifest object.

    Raises:
        QualificationIdentityError: The file is missing, not JSON, or not an object.
    """
    if not path.is_file():
        raise QualificationIdentityError(f"canonical manifest missing: {path}")
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise QualificationIdentityError(f"canonical manifest malformed: {path}: {exc}") from exc
    if not isinstance(data,dict):
        raise QualificationIdentityError(f"canonical manifest malformed: {path}: expected a JSON object")
    return data

def require_text(manifest:dict,key:str,path:Path)->str:
    """Return a required non-empty string field from a manifest.

    Args:
        manifest: Parsed manifest object.
        key: Required field name.
        path: Manifest path included in the error.

    Returns:
        The field value with surrounding whitespace removed.

    Raises:
        QualificationIdentityError: The field is missing, not a string, or blank.
    """
    value=manifest.get(key)
    if not isinstance(value,str) or not value.strip():
        raise QualificationIdentityError(f"canonical manifest malformed: {path}: {key} must be a non-empty string")
    return value.strip()

def canonical_identity(root:Path)->dict:
    """Load product, schema, and MediaMTX identity from the canonical manifests.

    Args:
        root: Product root.

    Returns:
        Identity fields used to stamp a qualification run. MediaMTX pins are the
        Windows zip and exe digests from runtime.json. They are not copied into
        a run unless an artifact is hashed and matches one of them.

    Raises:
        QualificationIdentityError: A manifest is missing or malformed.
    """
    product_path=root/PRODUCT_MANIFEST
    runtime_path=root/RUNTIME_MANIFEST
    product=read_manifest(product_path)
    runtime=read_manifest(runtime_path)
    identity={dest:require_text(product,source,product_path) for dest,source in PRODUCT_FIELDS}
    identity["mediamtx_version"]=require_text(runtime,"runtime_version",runtime_path)
    for pin in RUNTIME_PINS:
        digest=require_text(runtime,pin,runtime_path)
        if not re.fullmatch(r"[0-9a-f]{64}",digest):
            raise QualificationIdentityError(
                f"canonical manifest malformed: {runtime_path}: {pin} must be a lowercase SHA-256")
        identity[pin]=digest
    return identity

def file_sha256(path:Path)->str:
    """Hash a file with SHA-256.

    Args:
        path: File to read.

    Returns:
        Lowercase hexadecimal SHA-256 of the file contents.
    """
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def mediamtx_stamp(identity:dict, artifact:str|None)->dict:
    """Build the MediaMTX identity stamp for a run.

    Args:
        identity: Canonical identity, including runtime version and Windows pins.
        artifact: Optional path to a MediaMTX zip or exe. When omitted, the
            digest is marked not provided.

    Returns:
        MediaMTX version from the runtime manifest plus a computed digest, or
        an explicit not-provided marker.

    Raises:
        QualificationIdentityError: The artifact is missing, or its digest does
            not match windows_zip_sha256 or windows_exe_sha256.
    """
    version=identity["mediamtx_version"]
    if artifact is None:
        return {"version":version,"sha256":SHA256_NOT_PROVIDED,"sha256_status":SHA256_NOT_PROVIDED}
    if not artifact.strip():
        raise QualificationIdentityError("mediamtx artifact missing: blank path")
    path=Path(artifact)
    if not path.is_file():
        raise QualificationIdentityError(f"mediamtx artifact missing: {path}")
    digest=file_sha256(path)
    if digest not in {identity[pin] for pin in RUNTIME_PINS}:
        raise QualificationIdentityError(
            "observed identity mismatch: mediamtx artifact sha256 "
            f"{digest} does not match runtime manifest windows_zip_sha256 or windows_exe_sha256")
    return {"version":version,"sha256":digest,"sha256_status":SHA256_COMPUTED}

def reject_supplied_identity_mismatch(args, identity:dict)->None:
    """Reject caller-supplied identity that disagrees with the manifests.

    Args:
        args: Parsed new-run arguments. Omitted optional fields are not checks.
        identity: Canonical identity.

    Raises:
        QualificationIdentityError: One or more supplied values differ from the
            manifest. The stamp is never taken from the supplied value.
    """
    mismatches=[]
    for attr,field in SUPPLIED_IDENTITY_FIELDS:
        value=getattr(args,attr)
        if value is None:
            continue
        expected=identity[field]
        if value!=expected:
            mismatches.append(f"{field} {value!r} does not match manifest {expected!r}")
    if mismatches:
        raise QualificationIdentityError("supplied identity mismatch: "+"; ".join(mismatches))

def build_new_run(args, root:Path)->dict:
    """Stamp a simulated qualification run from the canonical manifests.

    Args:
        args: Parsed new-run arguments.
        root: Product root containing the canonical manifests.

    Returns:
        Qualification result labeled SIMULATED. Product, installer, server,
        client, schema, and MediaMTX version come from the manifests.

    Raises:
        QualificationIdentityError: A manifest is missing or malformed, or a
            supplied or observed identity does not match the manifest.
        ValueError: The run sequence is outside 1..999.
    """
    identity=canonical_identity(root)
    reject_supplied_identity_mismatch(args, identity)
    return {
     "qualification_run_id":run_id(args.sequence),
     "started_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
     "tester_id":"HOSTED_CI","machine_id":"HOSTED-CI","test_profile":"LAYER_A_SIMULATION",
     "build":{
      "git_sha":args.git_sha,
      "installer":Path(args.installer).name,
      "installer_version":identity["installer_version"],
      "installer_sha256":args.installer_sha256,
      "product":identity["product"],
      "product_version":identity["product_version"],
      "server_version":identity["server_version"],
      "client_version":identity["client_version"],
      "db_schema_revision":identity["db_schema_revision"],
      "mediamtx":mediamtx_stamp(identity, args.mediamtx_artifact),
     },
     "evidence_source":"SIMULATED","overall_result":"NOT_RUN","signing_status":"UNSIGNED_EXPECTED",
     "approval":{"automated_result":"NOT_RUN","tester_signoff":"NOT_RUN","independent_review":"NOT_RUN"},
     "results":[],
     "known_limitations":["External Windows 10/11, camera, soak, performance and production signing evidence pending"],
    }

def release_product(build:dict)->str:
    """Return the product name carried by a run, or the canonical manifest name.

    Args:
        build: Build object from a qualification result.

    Returns:
        Product name to stamp on the release manifest.

    Raises:
        QualificationIdentityError: The result omitted product and the canonical
            product manifest is missing or malformed.
    """
    product=build.get("product")
    if isinstance(product,str) and product.strip():
        return product
    return canonical_identity(product_root_from(None))["product"]

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
    for key in ("started_utc","tester_id","machine_id","test_profile"):
        if not str(d.get(key,"")).strip(): errors.append(f"{key} is required")
    if not str(b.get("installer_version","")).strip(): errors.append("build.installer_version is required")
    source=d.get("evidence_source")
    if source not in SOURCES: errors.append("invalid evidence_source")
    if d.get("overall_result","NOT_RUN") not in STATES: errors.append("invalid overall_result")
    approval=d.get("approval",{})
    for key in ("automated_result","tester_signoff","independent_review"):
        if not str(approval.get(key,"")).strip(): errors.append(f"approval.{key} is required")
    for row in d.get("results",[]):
        tid=str(row.get("test_id","")); state=row.get("state"); rsource=row.get("evidence_source",source)
        if state not in STATES: errors.append(f"{tid}: invalid state")
        if rsource not in SOURCES: errors.append(f"{tid}: invalid evidence_source")
        if state in {"BLOCKED_EXTERNAL","NOT_APPLICABLE"} and not str(row.get("reason","")).strip():
            errors.append(f"{tid}: reason required for {state}")
        if state in {"PASS","PASS_WITH_LIMITATION"} and tid.startswith(EXTERNAL_ONLY_PREFIXES) and rsource in {"HOSTED_CI","SIMULATED"}:
            errors.append(f"{tid}: {rsource} cannot create real external PASS")
        if state=="FAIL":
            for key in ("expected","actual","severity","retest_status"):
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
    """Build a release manifest from a qualification result.

    The product name comes from the result when the run stamped one. Otherwise
    it is loaded from the canonical product manifest.

    Args:
        d: Qualification result document.

    Returns:
        Release manifest bound to that result's build identity.

    Raises:
        QualificationIdentityError: The result has no product name and the
            canonical product manifest is missing or malformed.
        KeyError: The result has no build object.
    """
    b=d["build"]
    return {"product":release_product(b),"product_version":b.get("product_version"),"git_sha":b.get("git_sha"),
      "installer":b.get("installer"),"installer_version":b.get("installer_version"),"installer_sha256":b.get("installer_sha256"),"server_version":b.get("server_version"),
      "client_version":b.get("client_version"),"db_schema_revision":b.get("db_schema_revision"),"mediamtx":b.get("mediamtx"),
      "installer_tool":"NSIS 3.13","qualification_run_id":d.get("qualification_run_id"),
      "qualification_status":d.get("overall_result"),"signing_status":d.get("signing_status","UNSIGNED_EXPECTED"),
      "known_limitations":d.get("known_limitations",[])}

def pe_has_authenticode(path:Path)->bool:
    data=path.read_bytes()
    if len(data)<0x40 or data[:2]!=b"MZ":
        return False
    pe_off=struct.unpack_from("<I",data,0x3C)[0]
    if pe_off+24>len(data) or data[pe_off:pe_off+4]!=b"PE\x00\x00":
        return False
    opt_off=pe_off+24
    magic=struct.unpack_from("<H",data,opt_off)[0]
    if magic==0x10B:
        directory_off=opt_off+96
    elif magic==0x20B:
        directory_off=opt_off+112
    else:
        return False
    security_entry=directory_off+(4*8)
    if security_entry+8>len(data):
        return False
    cert_offset,cert_size=struct.unpack_from("<II",data,security_entry)
    return cert_offset>0 and cert_size>0 and cert_offset+cert_size<=len(data)

# One table for both ConvertTo-Json forms of SignatureStatus. Windows PowerShell
# 5.1 emits the enum as its numeric value; Status.ToString() emits the name.
# https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.signaturestatus?view=powershellsdk-7.4.0
SIGNATURE_STATUS_BY_VALUE={
    0:"Valid",
    1:"UnknownError",
    2:"NotSigned",
    3:"HashMismatch",
    4:"NotTrusted",
    5:"NotSupportedFileFormat",
    6:"Incompatible",
}
_SIGNATURE_STATUS_BY_NAME={name:name for name in SIGNATURE_STATUS_BY_VALUE.values()}
_SIGNATURE_STATUS_BY_CODE={str(code):name for code,name in SIGNATURE_STATUS_BY_VALUE.items()}

def signature_status_name(status:object)->str|None:
    """Map one Authenticode Status value onto Microsoft's SignatureStatus name.

    Args:
        status: Status field from ConvertTo-Json. Integers and canonical decimal
            strings use SIGNATURE_STATUS_BY_VALUE. Enum names use that table's
            names. Booleans are rejected first because bool is a subclass of int
            and JSON false must not become Valid (0).

    Returns:
        The documented status name, or None when the value is outside the enum.

    Raises:
        This function does not raise.
    """
    if isinstance(status,bool) or not isinstance(status,(int,str)):
        return None
    if isinstance(status,int):
        return SIGNATURE_STATUS_BY_VALUE.get(status)
    if status in _SIGNATURE_STATUS_BY_NAME:
        return status
    return _SIGNATURE_STATUS_BY_CODE.get(status)

def authenticode_status_verdict(status:object, expect_unsigned:bool)->str:
    """Classify a certificate-table artifact from one SignatureStatus value.

    Args:
        status: Raw Status value from Get-AuthenticodeSignature JSON.
        expect_unsigned: When true, NotSigned and UnknownError stay on the
            existing unsigned path. Valid is never treated as unsigned.

    Returns:
        SIGNED_VALID, UNSIGNED_EXPECTED, or FAIL. Unmapped values are FAIL.

    Raises:
        This function does not raise.
    """
    name=signature_status_name(status)
    if name=="Valid":
        return "SIGNED_VALID"
    if expect_unsigned and name in {"NotSigned","UnknownError"}:
        return "UNSIGNED_EXPECTED"
    return "FAIL"

def _authenticode_verification_available()->bool:
    """Return whether Get-AuthenticodeSignature can run on this host.

    Returns:
        True when os.name is nt.

    Raises:
        This function does not raise.
    """
    return os.name=="nt"

def verify_signature(path:str, expect_unsigned:bool)->tuple[str,str]:
    """Verify Authenticode status without deciding publisher or timestamp policy.

    A PE with no certificate table follows the unsigned field-test path when
    the caller expected that. A certificate table is checked with
    Get-AuthenticodeSignature on Windows. Status is mapped through
    SIGNATURE_STATUS_BY_VALUE, so numeric 0 and the name Valid are both signed.
    Publisher identity and timestamp checks stay owner decisions.

    Args:
        path: Artifact path to inspect.
        expect_unsigned: When true, a missing certificate table, NotSigned, or
            UnknownError is UNSIGNED_EXPECTED.

    Returns:
        A status and detail pair. Status is SIGNED_VALID, UNSIGNED_EXPECTED,
        NOT_RUN, or FAIL.

    Raises:
        OSError: The artifact cannot be read.
        subprocess.TimeoutExpired: PowerShell does not finish within 30 seconds.
    """
    artifact=Path(path)
    has_signature=pe_has_authenticode(artifact)
    if not has_signature:
        return ("UNSIGNED_EXPECTED" if expect_unsigned else "FAIL","PE has no Authenticode certificate table")
    if not _authenticode_verification_available():
        return "NOT_RUN","Authenticode cryptographic verification requires Windows"
    escaped=str(Path(path)).replace("'","''")
    ps=(
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        "(Get-AuthenticodeSignature -LiteralPath '"+escaped+"') | "
        "Select-Object @{Name='Status';Expression={$_.Status.ToString()}},"
        "StatusMessage,SignerCertificate,TimeStamperCertificate | "
        "ConvertTo-Json -Compress -Depth 4"
    )
    winps=Path(os.environ.get("SystemRoot",r"C:\\Windows"))/"System32"/"WindowsPowerShell"/"v1.0"/"powershell.exe"
    exe=str(winps) if winps.exists() else "powershell.exe"
    cp=subprocess.run([exe,"-NoProfile","-NonInteractive","-Command",ps],capture_output=True,text=True,timeout=30)
    if cp.returncode:
        return "FAIL",cp.stderr.strip()
    try:
        data=json.loads(cp.stdout)
    except json.JSONDecodeError:
        return "FAIL","Authenticode status output was not valid JSON"
    if not isinstance(data,dict):
        return "FAIL","Authenticode status output was not a JSON object"
    detail=json.dumps(data,sort_keys=True)
    return authenticode_status_verdict(data.get("Status"),expect_unsigned),detail

def main():
    """Dispatch qualification harness commands.

    Returns:
        Process exit code. new-run returns 1 when canonical identity cannot be stamped.
    """
    p=argparse.ArgumentParser(); sp=p.add_subparsers(dest="cmd",required=True)
    n=sp.add_parser("new-run")
    n.add_argument("--sequence",type=int,required=True)
    n.add_argument("--git-sha",required=True)
    n.add_argument("--installer",required=True)
    n.add_argument("--installer-sha256",required=True)
    n.add_argument("--output",required=True)
    n.add_argument("--product-root",help="Product root containing the canonical identity manifests")
    n.add_argument("--product-version",help="Observed product version; must match the manifest")
    n.add_argument("--installer-version",help="Observed installer version; must match the manifest")
    n.add_argument("--server-version",help="Observed server version; must match the manifest")
    n.add_argument("--client-version",help="Observed client version; must match the manifest")
    n.add_argument("--schema-revision",help="Observed schema revision; must match the manifest")
    n.add_argument("--mediamtx-version",help="Observed MediaMTX runtime version; must match the manifest")
    n.add_argument("--mediamtx-artifact",help="MediaMTX zip or exe to hash; required to record a digest")
    v=sp.add_parser("validate"); v.add_argument("--result",required=True)
    s=sp.add_parser("summarize"); s.add_argument("--result",required=True); s.add_argument("--output",required=True)
    r=sp.add_parser("release-manifest"); r.add_argument("--result",required=True); r.add_argument("--output",required=True)
    x=sp.add_parser("redact"); x.add_argument("--input",required=True); x.add_argument("--output",required=True)
    a=sp.add_parser("verify-signature"); a.add_argument("--artifact",required=True); a.add_argument("--expect-unsigned",action="store_true")
    perf=sp.add_parser("performance-template"); perf.add_argument("--output",required=True)
    args=p.parse_args()
    if args.cmd=="new-run":
        try:
            stamped=build_new_run(args, product_root_from(args.product_root))
        except QualificationIdentityError as exc:
            print(f"qualification_identity_error: {exc}", file=sys.stderr)
            return 1
        Path(args.output).write_text(json.dumps(stamped,indent=2)+"\n",encoding="utf-8"); return 0
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
