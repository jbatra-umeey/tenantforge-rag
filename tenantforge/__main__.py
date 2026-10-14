import argparse
import threading

from .auth import Auth
from .config import Settings
from .db import Database
from .providers import Providers
from .storage import Storage
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description="TenantForge RAG")
    parser.add_argument("command", choices=["init", "token", "worker", "serve", "demo", "purge"])
    parser.add_argument("--tenant", default="acme")
    parser.add_argument("--user", default="alice")
    parser.add_argument("--role", default="editor", choices=["reader", "editor", "admin"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    s = Settings.from_env()
    db = Database(s)
    if args.command == "init":
        db.initialize()
        db.provision(args.tenant)
        print(f"Initialized schema and tenant {args.tenant}")
    elif args.command == "token":
        print(Auth(s).issue(args.tenant, args.user, args.role))
    elif args.command == "worker":
        from prometheus_client import start_http_server
        from .telemetry import configure
        configure(s)
        # Worker metrics are internal-only; do not expose this port publicly.
        start_http_server(9101, addr="0.0.0.0")
        providers = Providers(s)
        try:
            Worker(db, Storage(s), providers).run()
        finally:
            providers.close()
    elif args.command == "purge":
        from .maintenance import purge
        print(purge(db, Storage(s), args.tenant, args.user))
    else:
        import uvicorn
        from .api import create_app
        stop, thread, providers = threading.Event(), None, None
        if args.command == "demo":
            if s.environment != "development" or db.pg:
                raise ValueError("Demo requires development SQLite mode")
            db.initialize()
            db.provision(args.tenant)
            print("Paste this short-lived development token into the dashboard:", flush=True)
            print(Auth(s).issue(args.tenant, args.user, args.role), flush=True)
            providers = Providers(s)
            thread = threading.Thread(target=Worker(db, Storage(s), providers).run, args=(stop,), daemon=True)
            thread.start()
        try:
            uvicorn.run(create_app(s), host=args.host, port=args.port, log_level="warning", access_log=False)
        finally:
            stop.set()
            if thread:
                thread.join(timeout=55)
            if providers:
                providers.close()


if __name__ == "__main__":
    main()

