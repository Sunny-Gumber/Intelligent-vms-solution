#!/usr/bin/env python3
"""Safe stdlib-only qualification evidence tooling."""
from __future__ import annotations
import argparse, csv, ctypes, datetime as dt, errno, hashlib, json, os, re, stat, struct, subprocess, sys, tempfile
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

_UNSIGNED_DETAIL="PE has no Authenticode certificate table"
_PE32_MAGIC=0x10B
_PE32PLUS_MAGIC=0x20B
_SECURITY_DIRECTORY_INDEX=4
_WIN_CERTIFICATE_HEADER=8
_CERTIFICATE_ALIGNMENT=8
# st_size above this is rejected before any read. 512 MiB covers a field-test
# installer and stops a sparse multi-hundred-megabyte file from being loaded.
_MAX_ARTIFACT_BYTES=512*1024*1024
# Header fields materialized for one classification. The certificate blob is
# not part of this budget.
_MAX_PE_HEADER_BYTES=1024*1024
_SNAPSHOT_CHUNK=1024*1024

def _in_file(length:int, offset:int, size:int)->bool:
    """Return whether offset+size lies inside a buffer without wrapping.

    Args:
        length: Buffer length in bytes.
        offset: Start offset.
        size: Byte count.

    Returns:
        True when both values are non-negative and the span fits.

    Raises:
        This function does not raise.
    """
    if offset<0 or size<0 or offset>length:
        return False
    return size<=length-offset

def _optional_header_layout(magic:int)->tuple[int,int,int]|None:
    """Return PE32 or PE32+ offsets for the security data directory.

    Args:
        magic: Optional-header magic.

    Returns:
        NumberOfRvaAndSizes offset, data-directory offset, and the optional
        header size required to include security directory index 4. None when
        magic is not PE32 (0x10B) or PE32+ (0x20B).

    Raises:
        This function does not raise.
    """
    if magic==_PE32_MAGIC:
        rva_offset,directory_offset=92,96
    elif magic==_PE32PLUS_MAGIC:
        rva_offset,directory_offset=108,112
    else:
        return None
    security_offset=directory_offset+(_SECURITY_DIRECTORY_INDEX*8)
    return rva_offset,directory_offset,security_offset+8

class _HeaderWindowExceeded(Exception):
    """Classification asked for more header bytes than the parse budget allows."""

class _ShortRead(Exception):
    """A regular file ended before the size recorded by fstat."""

class _SizeZeroUnbounded(Exception):
    """A descriptor with st_size 0 still returned more than the header budget."""

class _ByteView:
    """File-sized view that reads only the header spans classification asks for.

    Args:
        fd: Open descriptor positioned by the caller. This view seeks as needed.
        file_size: Logical size from fstat. Certificate bounds use this, not the
            number of bytes pulled into memory.
    """

    def __init__(self, fd:int, file_size:int):
        self.fd=fd
        self.file_size=file_size
        self.bytes_read=0
        self._cache:dict[tuple[int,int],bytes]={}

    def span(self, offset:int, size:int)->bytes|None:
        """Return one in-file span, or None when the span is outside the file.

        Args:
            offset: Start offset.
            size: Byte count.

        Returns:
            The bytes, or None when they are not inside the file.

        Raises:
            _HeaderWindowExceeded: The span would push materialized header
                bytes past _MAX_PE_HEADER_BYTES.
            _ShortRead: The descriptor returned fewer bytes than fstat promised.
        """
        if not _in_file(self.file_size, offset, size):
            return None
        key=(offset, size)
        cached=self._cache.get(key)
        if cached is not None:
            return cached
        if size>_MAX_PE_HEADER_BYTES or self.bytes_read>_MAX_PE_HEADER_BYTES-size:
            raise _HeaderWindowExceeded()
        os.lseek(self.fd, offset, os.SEEK_SET)
        blob=_read_exact(self.fd, size)
        self._cache[key]=blob
        self.bytes_read+=size
        return blob

    def spans(self)->tuple[tuple[tuple[int,int],bytes],...]:
        """Return the header spans already read, in insertion order.

        Returns:
            Offset, size, and bytes for each span.

        Raises:
            This function does not raise.
        """
        return tuple(self._cache.items())

def _read_exact(fd:int, size:int)->bytes:
    """Read exactly size bytes from the current descriptor position.

    Args:
        fd: Open descriptor.
        size: Byte count.

    Returns:
        The bytes.

    Raises:
        _ShortRead: EOF arrived before size bytes.
    """
    buf=bytearray()
    while len(buf)<size:
        chunk=os.read(fd, size-len(buf))
        if not chunk:
            raise _ShortRead()
        buf+=chunk
    return bytes(buf)

def _write_exact(fd:int, blob:bytes)->None:
    """Write every byte of blob.

    Args:
        fd: Open descriptor.
        blob: Bytes to write.

    Raises:
        OSError: The write returns no progress.
    """
    view=memoryview(blob)
    while view:
        wrote=os.write(fd, view)
        if wrote<=0:
            raise OSError(errno.EIO, "short write")
        view=view[wrote:]

def _artifact_open_flags()->int:
    """Return read flags that do not block on a FIFO and are not inherited.

    Returns:
        os.O_RDONLY plus O_CLOEXEC and O_NONBLOCK when this platform defines them.

    Raises:
        This function does not raise.
    """
    flags=os.O_RDONLY
    flags|=getattr(os, "O_CLOEXEC", 0)
    flags|=getattr(os, "O_NONBLOCK", 0)
    return flags

def _errno_name(err:int|None)->str:
    """Return the stable errno name for an OSError.

    Args:
        err: OSError.errno. None becomes UNKNOWN.

    Returns:
        A name such as ENOENT, EACCES, ELOOP, or EISDIR.

    Raises:
        This function does not raise.
    """
    if err is None:
        return "UNKNOWN"
    return errno.errorcode.get(err, "UNKNOWN")

class _Opened:
    """One artifact descriptor, or a fail-closed reason found before parsing."""

    def __init__(self, fd:int, view:_ByteView|None, fail:str|None):
        self.fd=fd
        self.view=view
        self.fail=fail

    def close(self)->None:
        """Close the descriptor if it is still open.

        Raises:
            OSError: close fails.
        """
        if self.fd>=0:
            os.close(self.fd)
            self.fd=-1

def _stat_nonregular_reason(path:str)->str|None:
    """Return a fail-closed reason when stat shows the path is not a regular file.

    Args:
        path: Path whose open already failed.

    Returns:
        ARTIFACT_UNREADABLE:EISDIR for a directory, ARTIFACT_NOT_REGULAR_FILE
        for a socket, FIFO, or device, or None when stat fails or the path is
        a regular file. The caller's open errno is kept in that last case.

    Raises:
        This function does not raise.
    """
    try:
        st=os.stat(path)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return "ARTIFACT_UNREADABLE:EISDIR"
    if not stat.S_ISREG(st.st_mode):
        return "ARTIFACT_NOT_REGULAR_FILE"
    return None

def _open_artifact(path:str)->_Opened:
    """Open one artifact, require a bounded regular file, and do not read it yet.

    Symlinks are followed. A reported size of 0 is not the empty placeholder
    until a later bounded read returns no bytes. Directories, devices, FIFOs,
    and sockets are rejected here. When open itself fails, stat still maps a
    socket, FIFO, or device to ARTIFACT_NOT_REGULAR_FILE. Unix O_NONBLOCK
    keeps a FIFO from blocking in open.

    Args:
        path: Artifact path.

    Returns:
        An open descriptor. fail is ARTIFACT_UNREADABLE:EISDIR,
        ARTIFACT_NOT_REGULAR_FILE, or ARTIFACT_TOO_LARGE when the path must
        not be parsed. The caller closes the descriptor.

    Raises:
        OSError: The path cannot be opened and stat does not show a non-regular file.
    """
    try:
        fd=os.open(path, _artifact_open_flags())
    except OSError as exc:
        reason=_stat_nonregular_reason(path)
        if reason is not None:
            return _Opened(-1, None, reason)
        raise exc
    try:
        st=os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            return _Opened(fd, None, "ARTIFACT_UNREADABLE:EISDIR")
        if not stat.S_ISREG(st.st_mode):
            return _Opened(fd, None, "ARTIFACT_NOT_REGULAR_FILE")
        if st.st_size>_MAX_ARTIFACT_BYTES:
            return _Opened(fd, None, "ARTIFACT_TOO_LARGE")
        return _Opened(fd, _ByteView(fd, st.st_size), None)
    except Exception:
        os.close(fd)
        raise

def _authenticode_container(view:_ByteView)->tuple[str,str]:
    """Classify a file before Authenticode status is trusted.

    UNSIGNED_EXPECTED is allowed only when a bounded read of a regular file is
    empty, and for a well-formed PE32/PE32+ whose security directory is exactly
    (0, 0). st_size 0 is not enough: procfs and sysfs can report 0 and still
    return bytes. Every
    header offset required to read that directory must lie inside the file.
    A non-zero directory that is truncated, misaligned, or past EOF is rejected
    here so it cannot skip PowerShell. Only the header fields are read. The
    certificate blob is checked against the fstat size and is not loaded.

    Args:
        view: Bounded view of a regular file.

    Returns:
        A kind and detail pair. Kind is empty, unsigned_pe, certificate, or
        reject. Reject details are NOT_A_PE_FILE, PE_SECURITY_DIR_OUT_OF_RANGE,
        or PE_MALFORMED:<reason>, including header_exceeds_parse_window.

    Raises:
        _ShortRead: A header span ended before fstat said it was present.
    """
    try:
        return _classify_authenticode(view)
    except _HeaderWindowExceeded:
        return "reject","PE_MALFORMED:header_exceeds_parse_window"

class _MemoryView:
    """In-memory view of bytes actually read from a size-0 descriptor."""

    def __init__(self, data:bytes):
        self.file_size=len(data)
        self.bytes_read=0
        self._data=data
        self._cache:dict[tuple[int,int],bytes]={}

    def span(self, offset:int, size:int)->bytes|None:
        """Return one span from the buffered read.

        Args:
            offset: Start offset.
            size: Byte count.

        Returns:
            The bytes, or None when they are outside the buffer.

        Raises:
            _HeaderWindowExceeded: The span exceeds the parse budget.
        """
        if not _in_file(self.file_size, offset, size):
            return None
        key=(offset, size)
        cached=self._cache.get(key)
        if cached is not None:
            return cached
        if size>_MAX_PE_HEADER_BYTES or self.bytes_read>_MAX_PE_HEADER_BYTES-size:
            raise _HeaderWindowExceeded()
        blob=self._data[offset:offset+size]
        self._cache[key]=blob
        self.bytes_read+=size
        return blob

    def spans(self)->tuple[tuple[tuple[int,int],bytes],...]:
        """Return the spans already served.

        Returns:
            Offset, size, and bytes for each span.

        Raises:
            This function does not raise.
        """
        return tuple(self._cache.items())

def _read_up_to(fd:int, size:int)->bytes:
    """Read up to size bytes, stopping at EOF.

    Args:
        fd: Open descriptor.
        size: Maximum byte count.

    Returns:
        The bytes that were available, which may be shorter than size.

    Raises:
        OSError: The read fails.
    """
    buf=bytearray()
    while len(buf)<size:
        chunk=os.read(fd, size-len(buf))
        if not chunk:
            break
        buf+=chunk
    return bytes(buf)

def _read_size_zero(fd:int)->bytes:
    """Read a descriptor whose st_size is 0. Emptiness is the read, not the size.

    A non-PE prefix stops after two bytes so a procfs or sysfs file is not
    loaded further. An MZ prefix is read only up to the header budget.

    Args:
        fd: Open descriptor positioned at the start.

    Returns:
        b"" when the read is empty, otherwise the bounded prefix.

    Raises:
        _SizeZeroUnbounded: Another byte exists past the header budget.
        OSError: The read fails.
    """
    first=_read_up_to(fd, 2)
    if len(first)<2 or not first.startswith(b"MZ"):
        return first
    buf=bytearray(first)
    while len(buf)<_MAX_PE_HEADER_BYTES:
        chunk=os.read(fd, min(65536, _MAX_PE_HEADER_BYTES-len(buf)))
        if not chunk:
            return bytes(buf)
        buf+=chunk
    if os.read(fd, 1):
        raise _SizeZeroUnbounded()
    return bytes(buf)

def _classify_authenticode(view:_ByteView)->tuple[str,str]:
    """Apply the PE security-directory decision order to a bounded view.

    Args:
        view: Bounded view of a regular file.

    Returns:
        The kind and detail pair from _authenticode_container.

    Raises:
        _HeaderWindowExceeded: A header span exceeds the parse budget.
        _ShortRead: A header span cannot be read.
        OSError: A size-0 descriptor cannot be read.
    """
    if view.file_size==0:
        try:
            data=_read_size_zero(view.fd)
        except _SizeZeroUnbounded:
            return "reject","ARTIFACT_TOO_LARGE"
        if data==b"":
            return "empty",_UNSIGNED_DETAIL
        return _classify_authenticode(_MemoryView(data))
    length=view.file_size
    lead_n=2 if length>=2 else length
    lead=view.span(0, lead_n)
    if lead is None or not lead.startswith(b"MZ"):
        return "reject","NOT_A_PE_FILE"
    if not _in_file(length, 0, 0x40):
        return "reject","PE_MALFORMED:truncated_dos_header"
    dos=view.span(0, 0x40)
    if dos is None:
        return "reject","PE_MALFORMED:truncated_dos_header"
    pe_off=struct.unpack_from("<I", dos, 0x3C)[0]
    if pe_off<0x40:
        return "reject","PE_MALFORMED:e_lfanew_overlaps_dos_header"
    if not _in_file(length, pe_off, 24):
        return "reject","PE_MALFORMED:e_lfanew_out_of_range"
    coff=view.span(pe_off, 24)
    if coff is None or coff[:4]!=b"PE\x00\x00":
        return "reject","PE_MALFORMED:missing_pe_signature"
    if not _in_file(length, pe_off+24, 2):
        return "reject","PE_MALFORMED:truncated_optional_header"
    magic_b=view.span(pe_off+24, 2)
    if magic_b is None:
        return "reject","PE_MALFORMED:truncated_optional_header"
    magic=struct.unpack("<H", magic_b)[0]
    layout=_optional_header_layout(magic)
    if layout is None:
        return "reject","PE_MALFORMED:unknown_optional_header_magic"
    rva_offset,directory_offset,min_optional=layout
    optional_size=struct.unpack_from("<H", coff, 20)[0]
    if optional_size<min_optional:
        return "reject","PE_MALFORMED:optional_header_excludes_security_directory"
    opt_off=pe_off+24
    security_rel=directory_offset+(_SECURITY_DIRECTORY_INDEX*8)
    if not _in_file(length, opt_off, security_rel+8):
        return "reject","PE_MALFORMED:truncated_security_directory"
    if not _in_file(length, opt_off, optional_size):
        return "reject","PE_MALFORMED:truncated_optional_header"
    if not _in_file(length, opt_off+rva_offset, 4):
        return "reject","PE_MALFORMED:truncated_optional_header"
    rva_b=view.span(opt_off+rva_offset, 4)
    if rva_b is None:
        return "reject","PE_MALFORMED:truncated_optional_header"
    directory_count=struct.unpack("<I", rva_b)[0]
    if directory_count<_SECURITY_DIRECTORY_INDEX+1:
        return "reject","PE_MALFORMED:security_directory_absent"
    if directory_count>0xFFFFFFFF//8:
        return "reject","PE_MALFORMED:data_directory_count_overflows"
    array_bytes=directory_count*8
    if directory_offset>optional_size or array_bytes>optional_size-directory_offset:
        return "reject","PE_MALFORMED:data_directory_array_exceeds_optional_header"
    cert_b=view.span(opt_off+security_rel, 8)
    if cert_b is None:
        return "reject","PE_MALFORMED:truncated_security_directory"
    cert_offset,cert_size=struct.unpack("<II", cert_b)
    if cert_offset==0 and cert_size==0:
        return "unsigned_pe",_UNSIGNED_DETAIL
    if cert_offset==0 or cert_size==0:
        return "reject","PE_MALFORMED:security_directory_incomplete"
    if cert_size>0xFFFFFFFF-cert_offset or not _in_file(length, cert_offset, cert_size):
        return "reject","PE_SECURITY_DIR_OUT_OF_RANGE"
    if cert_offset%_CERTIFICATE_ALIGNMENT!=0:
        return "reject","PE_MALFORMED:security_directory_misaligned"
    if cert_size<_WIN_CERTIFICATE_HEADER:
        return "reject","PE_MALFORMED:certificate_table_truncated"
    return "certificate",""

def pe_has_authenticode(path:Path)->bool:
    """Return whether the PE security directory names a certificate inside the file.

    Args:
        path: Artifact to inspect. The read is limited to header fields of a
            regular file at most _MAX_ARTIFACT_BYTES.

    Returns:
        True only when the certificate blob is fully inside the file. A zero
        security directory, a non-regular file, an oversize file, and every
        malformed container return False.

    Raises:
        OSError: The artifact cannot be opened.
    """
    opened=_open_artifact(str(path))
    try:
        if opened.fail or opened.view is None:
            return False
        kind,_detail=_authenticode_container(opened.view)
        return kind=="certificate"
    except _ShortRead:
        return False
    finally:
        opened.close()

def _json_object_no_duplicate_keys(pairs:list)->dict:
    """Build one JSON object and reject a repeated key.

    Args:
        pairs: Key and value pairs from json.loads object_pairs_hook.

    Returns:
        The object when every key appears once.

    Raises:
        ValueError: A key is repeated. Callers map that to SIGNATURE_STATUS_AMBIGUOUS.
    """
    obj={}
    for key,value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key]=value
    return obj

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

def signature_rejection_reason(status:object)->str:
    """Return the fail-closed reason code for one SignatureStatus value.

    Normalisation stays in signature_status_name. This function only names why
    a value is not an accepted verdict.

    Args:
        status: Raw Status field from ConvertTo-Json. None is missing. A blank
            or whitespace-only string is empty. Every value
            signature_status_name cannot map is unparsable.

    Returns:
        SIGNATURE_STATUS_MISSING, SIGNATURE_STATUS_EMPTY,
        SIGNATURE_STATUS_UNPARSED, or SIGNATURE_STATUS_REJECTED:<Name> using
        the canonical name from signature_status_name.

    Raises:
        This function does not raise.
    """
    if status is None:
        return "SIGNATURE_STATUS_MISSING"
    if isinstance(status,str) and status.strip()=="":
        return "SIGNATURE_STATUS_EMPTY"
    name=signature_status_name(status)
    if name is None:
        return "SIGNATURE_STATUS_UNPARSED"
    return "SIGNATURE_STATUS_REJECTED:"+name

def authenticode_status_verdict(status:object, expect_unsigned:bool)->str:
    """Classify a certificate-table artifact from one SignatureStatus value.

    Args:
        status: Raw Status value from Get-AuthenticodeSignature JSON.
        expect_unsigned: When true, only NotSigned is UNSIGNED_EXPECTED.
            UnknownError and every other non-Valid status fail closed. Valid
            is never treated as unsigned. Boss default pending signing ADR
            owner confirmation.

    Returns:
        SIGNED_VALID, UNSIGNED_EXPECTED, or FAIL. Unmapped, missing, and
        empty values are FAIL.

    Raises:
        This function does not raise.
    """
    name=signature_status_name(status)
    if name=="Valid":
        return "SIGNED_VALID"
    # UnknownError means the signature could not be read. It is not unsigned.
    if expect_unsigned and name=="NotSigned":
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

class _Snapshot:
    """Private byte-for-byte copy of the artifact PowerShell will verify."""

    def __init__(self, path:Path, directory:Path, digest:str, size:int):
        self.path=path
        self.directory=directory
        self.digest=digest
        self.size=size

    def cleanup(self)->None:
        """Remove the private copy.

        Raises:
            This function does not raise.
        """
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self.directory)
        except OSError:
            pass

def _snapshot_fd(fd:int, size:int)->_Snapshot:
    """Stream exactly size bytes from fd into a private file and hash them.

    Args:
        fd: Regular-file descriptor. It is seeked to the start.
        size: Byte count from the fstat used for classification. At most
            _MAX_ARTIFACT_BYTES.

    Returns:
        The private copy and the SHA-256 of the bytes written.

    Raises:
        _ShortRead: The descriptor ended before size bytes.
        OSError: The private file cannot be created or written.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    directory=Path(tempfile.mkdtemp(prefix="vms-authenticode-"))
    path=directory/"artifact.bin"
    out=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os, "O_CLOEXEC", 0), 0o600)
    digest=hashlib.sha256()
    try:
        remaining=size
        while remaining:
            chunk=os.read(fd, min(_SNAPSHOT_CHUNK, remaining))
            if not chunk:
                raise _ShortRead()
            digest.update(chunk)
            _write_exact(out, chunk)
            remaining-=len(chunk)
        os.fsync(out)
    except Exception:
        os.close(out)
        _Snapshot(path, directory, "", size).cleanup()
        raise
    os.close(out)
    try:
        os.chmod(path, 0o400)
    except OSError:
        _Snapshot(path, directory, "", size).cleanup()
        raise
    return _Snapshot(path, directory, digest.hexdigest(), size)

def _snapshot_matches(path:Path, view:_ByteView)->bool:
    """Return whether the private copy still has the classified header spans.

    Args:
        path: Private copy.
        view: View whose spans were classified.

    Returns:
        True when the copy is a regular file of the same size and every
        classified span matches.

    Raises:
        OSError: The copy cannot be opened.
    """
    fd=os.open(path, _artifact_open_flags())
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size!=view.file_size:
            return False
        for (offset, size), blob in view.spans():
            os.lseek(fd, offset, os.SEEK_SET)
            if _read_exact(fd, size)!=blob:
                return False
        return True
    except _ShortRead:
        return False
    finally:
        os.close(fd)

def _hash_regular_exact(path:Path, size:int)->str:
    """Hash a regular file that must still be exactly size bytes.

    Args:
        path: File to hash.
        size: Expected size. The file is not read when the size differs.

    Returns:
        Lowercase SHA-256.

    Raises:
        _ShortRead: The file is no longer a regular file of that size, or the
            read ended early.
        OSError: The file cannot be opened.
    """
    fd=os.open(path, _artifact_open_flags())
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size!=size or st.st_size>_MAX_ARTIFACT_BYTES:
            raise _ShortRead()
        digest=hashlib.sha256()
        remaining=size
        while remaining:
            chunk=os.read(fd, min(_SNAPSHOT_CHUNK, remaining))
            if not chunk:
                raise _ShortRead()
            digest.update(chunk)
            remaining-=len(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)

def _powershell_status(path:str, expect_unsigned:bool)->tuple[str,str]:
    """Run Get-AuthenticodeSignature on one path and map its Status.

    Args:
        path: File the cmdlet must read. Callers pass the private copy.
        expect_unsigned: Forwarded to authenticode_status_verdict.

    Returns:
        A status and detail pair. FAIL details keep the existing reason codes.

    Raises:
        subprocess.TimeoutExpired: PowerShell does not finish within 30 seconds.
    """
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
        data=json.loads(cp.stdout, object_pairs_hook=_json_object_no_duplicate_keys)
    except json.JSONDecodeError:
        return "FAIL","Authenticode status output was not valid JSON"
    except ValueError:
        return "FAIL","SIGNATURE_STATUS_AMBIGUOUS"
    if not isinstance(data, dict):
        return "FAIL","Authenticode status output was not a JSON object"
    raw_status=data.get("Status")
    verdict=authenticode_status_verdict(raw_status, expect_unsigned)
    detail=json.dumps(data, sort_keys=True)
    if verdict=="FAIL":
        detail=signature_rejection_reason(raw_status)+" "+detail
    return verdict, detail

def _hash_fd_exact(fd:int, size:int)->str:
    """Hash exactly size bytes from the current descriptor, starting at offset 0.

    Args:
        fd: Open regular-file descriptor. It is seeked to the start.
        size: Byte count to hash.

    Returns:
        Lowercase SHA-256.

    Raises:
        _ShortRead: The descriptor ended early.
        OSError: The seek or read fails.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    digest=hashlib.sha256()
    remaining=size
    while remaining:
        chunk=os.read(fd, min(_SNAPSHOT_CHUNK, remaining))
        if not chunk:
            raise _ShortRead()
        digest.update(chunk)
        remaining-=len(chunk)
    return digest.hexdigest()

class _HeldCopy:
    """Identity of the private copy, held open across the cmdlet."""

    def __init__(self, fd:int, dev:int, ino:int, size:int, mtime_ns:int, digest:str):
        self.fd=fd
        self.dev=dev
        self.ino=ino
        self.size=size
        self.mtime_ns=mtime_ns
        self.digest=digest

    def close(self)->None:
        """Close the held descriptor.

        Raises:
            OSError: close fails.
        """
        if self.fd>=0:
            os.close(self.fd)
            self.fd=-1

    def matches(self, path:Path)->bool:
        """Return whether path and this descriptor are still the hashed bytes.

        Args:
            path: Path the cmdlet was given.

        Returns:
            True when dev, inode, size, mtime_ns, and SHA-256 all still match.

        Raises:
            OSError: The held descriptor cannot be stat'd or read.
            _ShortRead: The re-hash ended early.
        """
        identity=(self.dev, self.ino, self.size, self.mtime_ns)
        st=os.fstat(self.fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)!=identity:
            return False
        try:
            path_st=os.stat(path)
        except OSError:
            return False
        if (path_st.st_dev, path_st.st_ino, path_st.st_size, path_st.st_mtime_ns)!=identity:
            return False
        if _hash_fd_exact(self.fd, self.size)!=self.digest:
            return False
        st2=os.fstat(self.fd)
        return (st2.st_dev, st2.st_ino, st2.st_size, st2.st_mtime_ns)==identity

def _hold_copy(path:Path, size:int, digest:str)->_HeldCopy:
    """Open path and bind dev, inode, size, mtime_ns, and the expected hash.

    Args:
        path: Private copy.
        size: Expected size.
        digest: SHA-256 recorded while the copy was written.

    Returns:
        A descriptor that the caller keeps open until after the cmdlet.

    Raises:
        _ShortRead: The file is not the expected regular bytes.
        OSError: The file cannot be opened.
    """
    fd=os.open(path, _artifact_open_flags())
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size!=size:
            raise _ShortRead()
        actual=_hash_fd_exact(fd, st.st_size)
        st2=os.fstat(fd)
        if actual!=digest or (st2.st_dev, st2.st_ino, st2.st_size, st2.st_mtime_ns)!=(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
            raise _ShortRead()
        return _HeldCopy(fd, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, digest)
    except Exception:
        os.close(fd)
        raise

# inotify event masks. Used only where libc provides the calls.
_IN_MODIFY=0x00000002
_IN_ATTRIB=0x00000004
_IN_CLOSE_WRITE=0x00000008
_IN_DELETE_SELF=0x00000400
_IN_MOVE_SELF=0x00000800

class _MutationWatch:
    """Linux inotify watch. A queued event means the copy was not quiet."""

    def __init__(self)->None:
        self.fd=-1

    def arm(self, path:str)->bool:
        """Watch path for writes and attribute changes.

        Args:
            path: Private copy path.

        Returns:
            True when the watch is armed. False when this platform has no
            inotify or the watch cannot be created.

        Raises:
            This function does not raise.
        """
        try:
            libc=ctypes.CDLL(None, use_errno=True)
            init=getattr(libc, "inotify_init1", None)
            add=getattr(libc, "inotify_add_watch", None)
            if init is None or add is None:
                return False
            init.argtypes=[ctypes.c_int]
            init.restype=ctypes.c_int
            add.argtypes=[ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            add.restype=ctypes.c_int
            flags=getattr(os, "O_NONBLOCK", 0)|getattr(os, "O_CLOEXEC", 0)
            fd=init(flags)
            if fd<0:
                return False
            mask=_IN_MODIFY|_IN_ATTRIB|_IN_CLOSE_WRITE|_IN_DELETE_SELF|_IN_MOVE_SELF
            watched=add(fd, os.fsencode(path), mask)
            if watched<0:
                os.close(fd)
                return False
            self.fd=fd
            return True
        except (AttributeError, OSError):
            if self.fd>=0:
                os.close(self.fd)
                self.fd=-1
            return False

    def saw_change(self)->bool:
        """Return whether any event was queued, including an overflow.

        Returns:
            True when the watch saw activity or could not be read. False when
            the queue is empty. An unarmed watch returns False; the caller
            decides whether that missing proof is acceptable.

        Raises:
            This function does not raise.
        """
        if self.fd<0:
            return False
        saw=False
        while True:
            try:
                data=os.read(self.fd, 4096)
            except BlockingIOError:
                return saw
            except OSError:
                return True
            if not data:
                return saw
            saw=True

    def close(self)->None:
        """Close the inotify descriptor.

        Raises:
            This function does not raise.
        """
        if self.fd>=0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd=-1

def _verify_with_snapshot(opened:_Opened, expect_unsigned:bool)->tuple[str,str]:
    """Verify the private copy of a certificate-table artifact.

    The cmdlet result is accepted only when an fd held open across the call
    still has the same dev, inode, size, mtime_ns, and SHA-256, and the path
    still names that inode. On Linux an inotify watch must also stay quiet, so
    a writer who restores the bytes and the timestamp is still rejected.

    Args:
        opened: Open regular file whose container kind is certificate.
        expect_unsigned: Forwarded to the status map.

    Returns:
        The PowerShell verdict, or FAIL / ARTIFACT_CHANGED when the copy does
        not match the classified bytes.

    Raises:
        _ShortRead: The source or the copy changed length during the snapshot.
        subprocess.TimeoutExpired: PowerShell does not finish within 30 seconds.
        OSError: The private copy cannot be created.
    """
    view=opened.view
    if view is None or not view.spans():
        return "FAIL","ARTIFACT_CHANGED"
    snap=_snapshot_fd(opened.fd, view.file_size)
    held=None
    watch=_MutationWatch()
    try:
        if not _snapshot_matches(snap.path, view):
            return "FAIL","ARTIFACT_CHANGED"
        held=_hold_copy(snap.path, snap.size, snap.digest)
        armed=watch.arm(str(snap.path))
        status,detail=_powershell_status(str(snap.path), expect_unsigned)
        if not held.matches(snap.path):
            return "FAIL","ARTIFACT_CHANGED"
        # A quiet inotify queue proves the path was not written during the
        # cmdlet, including a restore that puts st_mtime_ns back. When the
        # watch cannot be armed, the fd identity above is the proof, and a
        # same-user restore of both bytes and mtime is a documented residual.
        if armed and watch.saw_change():
            return "FAIL","ARTIFACT_CHANGED"
        return status,detail
    finally:
        if held is not None:
            held.close()
        watch.close()
        snap.cleanup()

def verify_signature(path:str, expect_unsigned:bool)->tuple[str,str]:
    """Verify Authenticode status without deciding publisher or timestamp policy.

    With --expect-unsigned, UNSIGNED_EXPECTED before PowerShell is only a
    regular file of length 0, or a well-formed PE32/PE32+ whose security
    directory is exactly (0, 0). Every other container fails closed. An in-file
    certificate table is checked with Get-AuthenticodeSignature on Windows,
    against a private copy of the bytes that were classified. Status is mapped
    through SIGNATURE_STATUS_BY_VALUE, so numeric 0 and the name Valid are both
    signed. Publisher identity and timestamp checks stay owner decisions.

    Args:
        path: Artifact path to inspect.
        expect_unsigned: When true, only a regular file of length 0, a
            well-formed (0, 0) security directory, or NotSigned is
            UNSIGNED_EXPECTED. UnknownError is FAIL. Boss default pending
            signing ADR owner confirmation.

    Returns:
        A status and detail pair. Status is SIGNED_VALID, UNSIGNED_EXPECTED,
        NOT_RUN, or FAIL. Container failures use NOT_A_PE_FILE,
        PE_SECURITY_DIR_OUT_OF_RANGE, or PE_MALFORMED:<reason>. A certificate
        FAIL detail starts with SIGNATURE_STATUS_REJECTED:<Name>,
        SIGNATURE_STATUS_MISSING, SIGNATURE_STATUS_EMPTY,
        SIGNATURE_STATUS_UNPARSED, or SIGNATURE_STATUS_AMBIGUOUS. Unreadable
        paths use ARTIFACT_UNREADABLE:<errno-name>. Non-regular files use
        ARTIFACT_NOT_REGULAR_FILE. Oversize files use ARTIFACT_TOO_LARGE. A
        copy that does not match the classified bytes uses ARTIFACT_CHANGED.

    Raises:
        subprocess.TimeoutExpired: PowerShell does not finish within 30 seconds.
    """
    try:
        return _verify_signature_at(path, expect_unsigned)
    except OSError as exc:
        return "FAIL","ARTIFACT_UNREADABLE:"+_errno_name(exc.errno)
    except _ShortRead:
        return "FAIL","ARTIFACT_CHANGED"

def _verify_signature_at(path:str, expect_unsigned:bool)->tuple[str,str]:
    """Classify one opened artifact and, on Windows, verify its private copy.

    Args:
        path: Artifact path to inspect.
        expect_unsigned: Forwarded to the unsigned short-circuit and the status map.

    Returns:
        The status and detail pair from verify_signature.

    Raises:
        OSError: The artifact or its private copy cannot be opened.
        _ShortRead: The file changed length while it was being copied.
        subprocess.TimeoutExpired: PowerShell does not finish within 30 seconds.
    """
    opened=_open_artifact(path)
    try:
        if opened.fail:
            return "FAIL",opened.fail
        if opened.view is None:
            return "FAIL","ARTIFACT_CHANGED"
        kind,container_detail=_authenticode_container(opened.view)
        if kind in {"empty","unsigned_pe"}:
            return ("UNSIGNED_EXPECTED" if expect_unsigned else "FAIL"),container_detail
        if kind=="reject":
            return "FAIL",container_detail
        if not _authenticode_verification_available():
            return "NOT_RUN","Authenticode cryptographic verification requires Windows"
        return _verify_with_snapshot(opened, expect_unsigned)
    finally:
        opened.close()

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
