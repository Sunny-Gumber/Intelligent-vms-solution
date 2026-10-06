"""Verify actual MediaMTX RTSP/RTSPS, certificate trust and redirect confinement on loopback."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


SDP = ('v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Synthetic\r\nc=IN IP4 127.0.0.1\r\n'
       't=0 0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n'
       'a=fmtp:96 packetization-mode=1; sprop-parameter-sets=Z2QAC6w7ULBLQgAAAwACAAADAFkI,aM48gA==\r\n'
       'a=control:trackID=0\r\n')


class _Camera(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        # TLS rejection is expected in negative cases; never print transport details.
        pass


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.connections += 1
        sock = self.request
        sock.settimeout(3)
        if self.server.tls:
            sock = self.server.tls.wrap_socket(sock, server_side=True)
        with sock.makefile('rb') as stream:
            while True:
                line = stream.readline()
                if not line:
                    return
                method = line.decode().split(' ')[0]
                headers = {}
                while (row := stream.readline()) not in (b'\r\n', b''):
                    key, _, value = row.decode().partition(':')
                    headers[key.lower()] = value.strip()
                self.server.methods.append(method)
                status, extra, body = '200 OK', '', ''
                if method == 'DESCRIBE':
                    if self.server.redirect:
                        status, extra = '302 Found', 'Location: ' + self.server.redirect + '\r\n'
                    else:
                        extra, body = 'Content-Type: application/sdp\r\n', SDP
                elif method == 'SETUP':
                    extra = 'Session: synthetic-session\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n'
                elif method == 'PLAY':
                    extra = 'Session: synthetic-session\r\n'
                elif method == 'OPTIONS':
                    extra = 'Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n'
                response = f"RTSP/1.0 {status}\r\nCSeq: {headers.get('cseq', '1')}\r\n{extra}Content-Length: {len(body)}\r\n\r\n{body}"
                sock.sendall(response.encode())


def _certificate(work):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic loopback camera')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = work / 'cert.pem', work / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _camera(tls=None, redirect=None):
    server = _Camera(('127.0.0.1', 0), _Handler)
    server.tls, server.redirect, server.methods, server.connections = tls, redirect, [], 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _exercise(binary, work, scheme, pin, tls, *, redirect=False, expect_play=False, redirect_scheme=None):
    target_scheme = redirect_scheme or scheme
    sink = _camera(tls if target_scheme == 'rtsps' else None)
    source = _camera(tls, target_scheme + '://127.0.0.1:' + str(sink.server_address[1]) + '/escape' if redirect else None)
    secret = secrets.token_urlsafe(24)
    config = {'logLevel': 'error', 'rtsp': False, 'rtmp': False, 'hls': False, 'webrtc': False,
              'srt': False, 'moq': False, 'paths': {'synthetic': {
                  'source': f'{scheme}://synthetic:{secret}@127.0.0.1:{source.server_address[1]}/0',
                  'sourceFingerprint': pin or '', 'sourceOnDemand': False, 'rtspTransport': 'tcp'}}}
    path = work / 'runtime.json'
    path.write_text(json.dumps(config))
    process = subprocess.Popen([str(binary), str(path)], cwd=work,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline and process.poll() is None:
            if 'PLAY' in source.methods or redirect and 'DESCRIBE' in source.methods:
                break
            time.sleep(0.05)
        if process.poll() is not None:
            raise RuntimeError('runtime initialization failed')
        if expect_play and 'PLAY' not in source.methods:
            raise RuntimeError('trusted camera transport did not reach PLAY')
        if not expect_play and not redirect and source.methods:
            raise RuntimeError('untrusted camera transport was accepted')
        if redirect:
            time.sleep(0.5)
            if 'DESCRIBE' not in source.methods or sink.connections:
                raise RuntimeError('camera redirect confinement failed')
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate(timeout=5)[0]
        source.shutdown()
        source.server_close()
        sink.shutdown()
        sink.server_close()
        path.unlink(missing_ok=True)
    if secret.encode() in output:
        raise RuntimeError('runtime credential logging detected')


def run(binary: Path) -> None:
    """Test the deployed runtime using synthetic local cameras and ephemeral TLS keys.

    Args:
        binary: Actual MediaMTX executable being packaged for this deployment.

    Raises:
        RuntimeError: If transport, TLS verification, redirect or log boundaries fail.
    """
    with tempfile.TemporaryDirectory(prefix='vms-transport-') as directory:
        work = Path(directory)
        tls, fingerprint = _certificate(work)
        _exercise(binary, work, 'rtsp', None, None, expect_play=True)
        _exercise(binary, work, 'rtsps', fingerprint, tls, expect_play=True)
        _exercise(binary, work, 'rtsps', None, tls)
        _exercise(binary, work, 'rtsps', '0' * 64, tls)
        _exercise(binary, work, 'rtsps', fingerprint, None)
        _exercise(binary, work, 'rtsp', None, None, redirect=True)
        _exercise(binary, work, 'rtsps', fingerprint, tls, redirect=True)
        _exercise(binary, work, 'rtsps', fingerprint, tls, redirect=True, redirect_scheme='rtsp')
    print('camera_transport_runtime_ok rtsp=rtsps tls=verified redirects=blocked hardware=pending')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.binary.resolve())
    except Exception:
        raise SystemExit('camera_transport_runtime_failed; credential-bearing details suppressed') from None
