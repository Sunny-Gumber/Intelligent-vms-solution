#!/usr/bin/env python3
"""Mint a short-lived local field-test JWT from the private runtime .env."""
import argparse,base64,hashlib,hmac,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def env(path):
    out={}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line=raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k,v=line.split("=",1); out[k.strip()]=v.strip()
    return out
def b64(v): return base64.urlsafe_b64encode(json.dumps(v,separators=(",",":"),sort_keys=True).encode()).rstrip(b"=").decode()
def mint_token(secret,aud,sub,tenant,site,role,ttl):
    now=int(time.time()); h=b64({"alg":"HS256","typ":"JWT"}); p=b64({"sub":sub,"iat":now,"exp":now+ttl,"aud":aud,"roles":[role],"tenant_id":tenant,"site_ids":[site]}); msg=f"{h}.{p}"
    sig=base64.urlsafe_b64encode(hmac.new(secret.encode(),msg.encode(),hashlib.sha256).digest()).rstrip(b"=").decode(); return f"{msg}.{sig}"
def main():
    p=argparse.ArgumentParser(); p.add_argument("--env-file",type=Path,default=ROOT/".env"); p.add_argument("--subject",default="field-admin"); p.add_argument("--tenant",default="field-test"); p.add_argument("--site",default="site-01"); p.add_argument("--role",choices=["admin","operator","viewer"],default="admin"); p.add_argument("--ttl-seconds",type=int,default=3600); a=p.parse_args()
    if not 300<=a.ttl_seconds<=28800: raise SystemExit("ttl must be between 300 and 28800 seconds")
    v=env(a.env_file); secret=v.get("AUTH_HS256_SECRET","")
    if not secret or v.get("AUTH_DISABLED","").lower()!="false": raise SystemExit("field-test authentication is not configured")
    if v.get("AUTH_REQUIRE_OIDC","").lower()=="true": raise SystemExit("local field-test tokens are disabled when OIDC is required")
    print("Sensitive field-test token follows; paste it only into the VMS login screen.",file=sys.stderr)
    print(mint_token(secret,v.get("AUTH_AUDIENCE","intelligent-vms"),a.subject,a.tenant,a.site,a.role,a.ttl_seconds))
if __name__=="__main__": main()
