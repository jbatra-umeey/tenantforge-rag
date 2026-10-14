"""Start a disposable real server/worker and exercise HTTP, load and optional browser checks."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from tenantforge.auth import Auth
from tenantforge.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser",action="store_true")
    parser.add_argument("--port",type=int,default=8765)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    out = root / "artifacts"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        base = f"http://127.0.0.1:{args.port}"
        env = dict(os.environ,TF_DATABASE_URL=f"sqlite:///{folder}/db.sqlite",TF_STORAGE_DIR=f"{folder}/objects",
                   TF_TEST_TOKEN=Auth(Settings()).issue("acme"),TF_TEST_URL=base)
        with (out / "local-server.log").open("w") as log:
            server = subprocess.Popen([sys.executable,"-m","tenantforge","demo","--port",str(args.port)],
                                      env=env,stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False,timeout=2) as client:
                    for _ in range(100):
                        try:
                            if client.get(base+"/readyz").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if server.poll() is not None:
                            raise RuntimeError("Local server exited")
                        time.sleep(.1)
                    else:
                        raise RuntimeError("Local server startup timeout")
                for script,arguments,filename in [
                    ("http_smoke.py",[],"http-smoke.json"),
                    ("load_test.py",["--requests","20","--concurrency","4"],"load-probe.json")]:
                    result = subprocess.run([sys.executable,"scripts/"+script,"--base-url",base,*arguments],env=env,
                                            check=True,capture_output=True,text=True)
                    (out/filename).write_text(result.stdout)
                    print(result.stdout)
                if args.browser:
                    result = subprocess.run(["node","scripts/browser_smoke.cjs"],env=env,check=True,capture_output=True,text=True)
                    (out/"browser-smoke.json").write_text(result.stdout)
                    print(result.stdout)
            except subprocess.CalledProcessError as exc:
                print(exc.stdout,exc.stderr)
                raise
            finally:
                server.terminate()
                server.wait(timeout=20)
        # The demo prints a development token. Never package it or the server log.
        (out/"local-server.log").unlink()
    print(json.dumps({"local_verification":"complete"}))


if __name__ == "__main__":
    main()
