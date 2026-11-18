"""
Crate Boy phone link: talks to KP's PRIVATE data repo on GitHub.

- legend.json on the main branch: KP's legend, word list and rules (kept out of the public code).
- branch "inbox": the phone uploads voice notes and DJ-set parts there. The PC downloads them,
  then rewrites the branch WITHOUT those files (fresh orphan commit), so the audio doesn't pile up
  in the history. GitHub may keep unreachable data for a while before cleaning it up.
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import os
API = os.environ.get("CRATEBOY_GH_API", "https://api.github.com")


class GitHubError(Exception):
    pass


class Link:
    def __init__(self, owner, repo, token, log=print):
        self.owner, self.repo, self.token, self.log = owner.strip(), repo.strip(), token.strip(), log

    # ---------------------------------------------------------------- http
    def call(self, method, path, body=None, accept="application/vnd.github+json", raw=False, ok404=False):
        url = f"{API}/repos/{self.owner}/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "crate-boy"})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
                return b if raw else (json.loads(b) if b else {})
        except urllib.error.HTTPError as e:
            if e.code == 404 and ok404:
                return None
            msg = e.read().decode(errors="replace")[:200]
            if e.code == 401:
                raise GitHubError("GitHub says the token is wrong or expired.")
            if e.code in (403, 404):
                raise GitHubError(f"GitHub refused ({e.code}). Does the token have Contents read/write on {self.repo}?")
            raise GitHubError(f"GitHub error {e.code}: {msg}")
        except urllib.error.URLError as e:
            raise GitHubError(f"No connection to GitHub ({e.reason}).")

    def test(self):
        info = self.call("GET", "")
        return {"private": info.get("private"), "default": info.get("default_branch", "main")}

    # ---------------------------------------------------------------- legend
    def pull_legend(self, dest, last_sha=None):
        meta = self.call("GET", "/contents/legend.json", ok404=True)
        if meta is None:
            return None, "legend.json is not in the data repo yet"
        if meta.get("sha") == last_sha and Path(dest).exists():
            return last_sha, "unchanged"
        content = base64.b64decode(meta["content"]).decode("utf-8")
        json.loads(content)                                   # must be valid
        Path(dest).write_text(content, encoding="utf-8")
        return meta["sha"], "updated"

    # ---------------------------------------------------------------- inbox
    def inbox_files(self):
        tree = self.call("GET", "/git/trees/inbox?recursive=1", ok404=True)
        if tree is None:
            return None, []
        files = [t for t in tree.get("tree", []) if t["type"] == "blob" and t["path"].startswith("inbox/")]
        return tree["sha"], files

    def download_blob(self, sha, dest):
        b = self.call("GET", f"/git/blobs/{sha}", accept="application/vnd.github.raw+json", raw=True)
        Path(dest).write_bytes(b)

    def drop_from_inbox(self, paths):
        """Rewrite the inbox branch without `paths` as a fresh commit with no history."""
        ref = self.call("GET", "/git/ref/heads/inbox", ok404=True)
        if ref is None:
            return
        head = ref["object"]["sha"]
        commit = self.call("GET", f"/git/commits/{head}")
        tree = self.call("GET", f"/git/trees/{commit['tree']['sha']}?recursive=1")
        keep = [{"path": t["path"], "mode": t["mode"], "type": "blob", "sha": t["sha"]}
                for t in tree["tree"] if t["type"] == "blob" and t["path"] not in set(paths)]
        if not any(k["path"] == ".keep" for k in keep):
            blob = self.call("POST", "/git/blobs", {"content": "", "encoding": "utf-8"})
            keep.append({"path": ".keep", "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self.call("POST", "/git/trees", {"tree": keep})
        new_commit = self.call("POST", "/git/commits", {"message": "inbox emptied by Crate Boy", "tree": new_tree["sha"],
                                                         "parents": []})
        # only move the branch if the phone didn't upload something in the meantime
        again = self.call("GET", "/git/ref/heads/inbox")
        if again["object"]["sha"] != head:
            self.log("inbox changed during cleanup, retrying next round")
            return False
        self.call("PATCH", "/git/refs/heads/inbox", {"sha": new_commit["sha"], "force": True})
        return True


def pull_inbox(link, phone_dir, on_item, log=print):
    """Download every finished upload (audio + its .json) from the inbox branch.
    on_item(audio_path, meta) is called for each; returns the number pulled."""
    phone_dir = Path(phone_dir)
    phone_dir.mkdir(parents=True, exist_ok=True)
    _, files = link.inbox_files()
    by_path = {f["path"]: f for f in files}
    done = []
    for f in files:
        if not f["path"].endswith(".json"):
            continue
        base = f["path"][:-5]
        audio = next((by_path[p] for p in by_path if p.startswith(base + ".") and not p.endswith(".json")), None)
        if audio is None:
            continue                                        # audio still uploading
        meta_path = phone_dir / Path(f["path"]).name
        audio_path = phone_dir / Path(audio["path"]).name
        if not audio_path.exists():
            link.download_blob(audio["sha"], audio_path)
        link.download_blob(f["sha"], meta_path)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
        on_item(audio_path, meta)
        done += [f["path"], audio["path"]]
    if done:
        for _ in range(3):
            if link.drop_from_inbox(done):
                break
            time.sleep(2)
        log(f"phone: pulled {len(done) // 2} item(s)")
    return len(done) // 2
