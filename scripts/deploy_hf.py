"""Deploy IntelDoc to a free Hugging Face Space (Docker) backed by a free Neon Postgres.

Needs these in .env (never committed, never uploaded):
    HF_TOKEN=hf_...                 Hugging Face access token with WRITE permission
    NEON_DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require
    DEMO_PASSWORD=...               the browser asks for it (any username) - required for a public link
    HF_SPACE_NAME=inteldoc          optional, default "inteldoc"

    python scripts/deploy_hf.py            # check Neon, seed it, create/update the Space, set secrets, upload
    python scripts/deploy_hf.py --check    # only verify the credentials, change nothing
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

FRONT_MATTER = """---
title: IntelDoc
emoji: 🧾
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
pinned: false
short_description: Invoice PDF in, reasoned approve/review/reject out
---

"""

# Code only. Secrets, personal files, the dataset (downloaded during the image build) and local tools stay out.
IGNORE = [".env", ".env.*", "data/**", "tools/**", ".git/**", "**/__pycache__/**", "*.pyc", ".pytest_cache/**",
          "render.yaml", "README.md"]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}")
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify credentials only")
    args = ap.parse_args()

    env = dotenv_values(ROOT / ".env")
    token = env.get("HF_TOKEN") or ""
    neon = env.get("NEON_DATABASE_URL") or ""
    password = env.get("DEMO_PASSWORD") or ""
    groq = env.get("GROQ_API_KEY") or env.get("OPENAI_API_KEY") or ""
    space_name = env.get("HF_SPACE_NAME") or "inteldoc"

    missing = [k for k, v in {"HF_TOKEN": token, "NEON_DATABASE_URL": neon, "DEMO_PASSWORD": password,
                              "GROQ_API_KEY": groq}.items() if not v]
    if missing:
        fail(f"add to .env: {', '.join(missing)}")
    if "neon.tech" not in neon:
        fail("NEON_DATABASE_URL does not look like a Neon connection string")
    if len(password) < 8:
        fail("DEMO_PASSWORD should be at least 8 characters - the link will be public")

    from huggingface_hub import HfApi
    api = HfApi(token=token)

    # 1. credentials
    who = api.whoami()
    user = who["name"]
    repo_id = f"{user}/{space_name}"
    print(f"Hugging Face: logged in as {user}")

    import psycopg
    with psycopg.connect(neon, connect_timeout=20) as c:
        version = c.execute("SHOW server_version").fetchone()[0]
    print(f"Neon: connected (Postgres {version})")
    if args.check:
        print("Credentials OK - nothing changed.")
        return

    # 2. schema + master data into Neon (same seed as local)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py")], check=True,
                   env={**os.environ, "DATABASE_URL": neon})

    # 3. Space + secrets (secrets are encrypted by Hugging Face and never visible in the repo)
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
    for key, value in {"GROQ_API_KEY": groq, "DATABASE_URL": neon, "DEMO_PASSWORD": password}.items():
        api.add_space_secret(repo_id, key, value)
    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "LLM_VISION"):
        if env.get(key):
            api.add_space_secret(repo_id, key, env[key])
    print(f"Space {repo_id}: created/updated, secrets set")

    # 4. code + Space README (front matter tells Hugging Face to build the Dockerfile and use port 8000)
    api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=str(ROOT), ignore_patterns=IGNORE,
                      commit_message="Deploy IntelDoc")
    readme = FRONT_MATTER + (ROOT / "README.md").read_text(encoding="utf-8")
    api.upload_file(path_or_fileobj=readme.encode("utf-8"), path_in_repo="README.md", repo_id=repo_id,
                    repo_type="space", commit_message="Space README")

    host = re.sub(r"[^a-z0-9]+", "-", f"{user}-{space_name}".lower()).strip("-")
    print("\nUploaded. Hugging Face is now building the image (about 5-8 minutes).")
    print(f"  Build log : https://huggingface.co/spaces/{repo_id}")
    print(f"  App link  : https://{host}.hf.space   (password = DEMO_PASSWORD, any username)")


if __name__ == "__main__":
    main()
