"""Buffer GraphQL (https://developers.buffer.com) + Cloudinary as the temporary public host Buffer needs for video URLs.
Trimmed from Music_video/pipeline/buffer_publisher.py. Secrets only from env, never printed."""
import hashlib
import time

import requests

API = "https://api.buffer.com"
Q_ORGS = "query { account { organizations { id name } } }"
Q_CHANNELS = ("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
              "{ id name displayName service isDisconnected isLocked } }")
M_POST = """mutation($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id } }
  ... on MutationError { message } } }"""


class BufferError(Exception):
    pass


class Buffer:
    def __init__(self, key):
        self.key = key

    def gql(self, query, variables=None, retry=True):
        """retry=False for createPost: a timeout may mean the post was made, so it is never repeated (no duplicates)."""
        for attempt in range(3 if retry else 1):
            last = attempt == 2 or not retry
            try:
                r = requests.post(API, json={"query": query, "variables": variables or {}}, timeout=60,
                                  headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            except requests.RequestException as e:
                if last:
                    raise BufferError(f"network: {str(e)[:150]}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504) and not last:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise BufferError(f"HTTP {r.status_code}")
            body = r.json()
            if body.get("errors"):
                raise BufferError("; ".join(str(e.get("message", e))[:150] for e in body["errors"][:2]))
            return body["data"]

    def tiktok_channel(self):
        for org in self.gql(Q_ORGS)["account"]["organizations"]:
            for ch in self.gql(Q_CHANNELS, {"o": org["id"]})["channels"]:
                if str(ch.get("service", "")).lower() == "tiktok":
                    if ch["isDisconnected"] or ch["isLocked"]:
                        raise BufferError("TikTok channel is disconnected or locked in Buffer: reconnect it")
                    return ch  # shortcut: the first TikTok channel, upgrade to a channel_id in config when there are several
        raise BufferError("no TikTok channel connected to this Buffer account")

    def create_post(self, channel_id, text, url, mode):
        """mode: now (publish immediately) | queue (Buffer's own schedule) | draft."""
        inp = {"text": text, "channelId": channel_id, "schedulingType": "automatic",
               "mode": "shareNow" if mode == "now" else "addToQueue",
               "assets": [{"video": {"url": url, "metadata": {"thumbnailOffset": 2000}}}]}
        if mode == "draft":
            inp["saveToDraft"] = True
        res = self.gql(M_POST, {"input": inp}, retry=False)["createPost"]
        if "post" not in res:
            raise BufferError(res.get("message", "createPost failed")[:200])
        return res["post"]["id"]


class Cloudinary:
    def __init__(self, cloud, key, secret):
        self.cloud, self.key, self.secret = cloud, key, secret

    def upload(self, path, public_id):
        """shortcut: hosted files are never deleted (a ~10 MB clip x 3/day fits the free plan for years), add destroy when needed."""
        p = {"public_id": public_id, "overwrite": "true", "timestamp": int(time.time())}
        sig = hashlib.sha1(("&".join(f"{k}={v}" for k, v in sorted(p.items())) + self.secret).encode()).hexdigest()
        try:
            with open(path, "rb") as f:
                r = requests.post(f"https://api.cloudinary.com/v1_1/{self.cloud}/video/upload", data=dict(p, api_key=self.key, signature=sig),
                                  files={"file": f}, timeout=300)
        except requests.RequestException as e:
            raise BufferError(f"storage network: {str(e)[:150]}")
        if r.status_code != 200:
            raise BufferError(f"storage HTTP {r.status_code}")
        return r.json()["secure_url"]
