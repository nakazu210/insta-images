#!/usr/bin/env python3
"""GitHub Actions 上で動く予約投稿スクリプト（標準ライブラリのみ）

queue/*/job.json を確認し、予約時刻を過ぎたものを Instagram に投稿する。
投稿結果（posted / failed / missed）は results/<id>.json に書き出し、queue/<id>/ を削除する。
"""
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = ROOT / "queue"
RESULTS_DIR = ROOT / "results"
API = "https://graph.instagram.com/v22.0"
MAX_IMAGES = 10
MAX_CAPTION = 2200
MAX_HASHTAGS = 30
MISSED_HOURS = 6
JST = timezone(timedelta(hours=9))


class InstaError(Exception):
    pass


def die(msg):
    raise InstaError(msg)


def token():
    t = (os.environ.get("IG_ACCESS_TOKEN") or "").strip()
    if not t:
        die("IG_ACCESS_TOKEN が設定されていません")
    return t


def repo_name():
    r = (os.environ.get("REPO") or "nakazu210/insta-images").strip()
    if not r:
        die("REPO が設定されていません")
    return r


def api(method, path, params):
    url = f"{API}/{path}" if not path.startswith("http") else path
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        die(f"Instagram APIエラー ({e.code}): {body}")


def me():
    return api("GET", "me", {"fields": "user_id,username", "access_token": token()})


def check_caption(caption):
    caption = (caption or "").strip()
    if not caption:
        die("キャプションが空です")
    if len(caption) > MAX_CAPTION:
        die(f"キャプションが長すぎます（{len(caption)}文字 / 上限{MAX_CAPTION}文字）")
    if caption.count("#") > MAX_HASHTAGS:
        die(f"ハッシュタグが多すぎます（上限{MAX_HASHTAGS}個）")
    return caption


def wait_ready(container_id, tries=30, wait=3):
    for _ in range(tries):
        st = api("GET", container_id, {"fields": "status_code", "access_token": token()})
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") == "ERROR":
            die(f"Instagram側で画像・動画の処理に失敗しました: {st}")
        time.sleep(wait)
    die("Instagram側の処理がタイムアウトしました")


def permalink(media_id):
    # 公開はもう済んでいるので、リンクが取れなくても「投稿済み」として扱う（失敗扱いにすると二重投稿のもと）
    try:
        return api("GET", media_id, {"fields": "permalink", "access_token": token()}).get("permalink")
    except InstaError:
        return ""


def raw_url(did, filename):
    return f"https://raw.githubusercontent.com/{repo_name()}/main/queue/{urllib.parse.quote(did)}/{urllib.parse.quote(filename)}"


def post_carousel(uid, did, caption, images):
    caption = check_caption(caption)
    if not 1 <= len(images) <= MAX_IMAGES:
        die(f"写真は1〜{MAX_IMAGES}枚にしてください")
    urls = [raw_url(did, name) for name in images]
    if len(urls) == 1:
        c = api("POST", f"{uid}/media",
                {"image_url": urls[0], "caption": caption, "access_token": token()})
    else:
        children = []
        for url in urls:
            child = api("POST", f"{uid}/media",
                        {"image_url": url, "is_carousel_item": "true", "access_token": token()})
            wait_ready(child["id"])
            children.append(child["id"])
        c = api("POST", f"{uid}/media",
                {"media_type": "CAROUSEL", "children": ",".join(children),
                 "caption": caption, "access_token": token()})
    wait_ready(c["id"])
    result = api("POST", f"{uid}/media_publish", {"creation_id": c["id"], "access_token": token()})
    return permalink(result["id"])


def upload_direct(container, uri, video_path):
    """動画をInstagramへ直接送る（再開可能アップロード）"""
    data = Path(video_path).read_bytes()
    req = urllib.request.Request(uri or f"https://rupload.facebook.com/ig-api-upload/v22.0/{container}",
                                 data=data, method="POST",
                                 headers={"Authorization": f"OAuth {token()}", "offset": "0",
                                          "file_size": str(len(data))})
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        die(f"動画の送信に失敗しました ({e.code}): {e.read().decode(errors='replace')}")


def post_reel(uid, did, caption, video_name, job_dir):
    caption = check_caption(caption)
    video_path = job_dir / video_name
    if not video_path.exists():
        die(f"動画ファイルが見つかりません: {video_name}")
    try:
        c = api("POST", f"{uid}/media", {"media_type": "REELS", "upload_type": "resumable",
                                         "caption": caption, "access_token": token()})
        upload_direct(c["id"], c.get("uri"), video_path)
    except InstaError as e:
        print(f"直接の送信ができなかったため、公開URLで送信します: {str(e)[:200]}")
        c = api("POST", f"{uid}/media", {"media_type": "REELS", "video_url": raw_url(did, video_name),
                                         "caption": caption, "access_token": token()})
    wait_ready(c["id"], tries=100, wait=6)
    result = api("POST", f"{uid}/media_publish", {"creation_id": c["id"], "access_token": token()})
    return permalink(result["id"])


def parse_scheduled(s):
    """scheduled_at をタイムゾーン付きの UTC datetime に変換する"""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(timezone.utc)


def write_result(did, status, permalink=None, posted_at=None, error=None):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "id": did,
        "status": status,
        "permalink": permalink,
        "posted_at": posted_at,
        "error": error,
    }
    (RESULTS_DIR / f"{did}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_jobs():
    jobs = []
    if not QUEUE_DIR.exists():
        return jobs
    for p in sorted(QUEUE_DIR.glob("*/job.json")):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
            job["_dir"] = p.parent
            job["_at"] = parse_scheduled(job["scheduled_at"])
            jobs.append(job)
        except Exception as e:
            did = p.parent.name
            print(f"job.json の読み込みに失敗 ({did}): {e}")
            write_result(did, "failed", error=f"予約データの読み込みに失敗しました: {e}")
            shutil.rmtree(p.parent, ignore_errors=True)
    jobs.sort(key=lambda j: j["_at"])
    return jobs


def main():
    jobs = load_jobs()
    if not jobs:
        print("予約はありません")
        return

    now = datetime.now(timezone.utc)
    due = [j for j in jobs if j["_at"] <= now]
    if not due:
        print("現在時刻を過ぎた予約はありません")
        return

    uid = None
    for job in due:
        did = job.get("id") or job["_dir"].name
        job_dir = job["_dir"]
        at_utc = job["_at"]
        at_jst = at_utc.astimezone(JST)
        label = job.get("label") or did
        try:
            if now - at_utc > timedelta(hours=MISSED_HOURS):
                msg = f"予約時刻（{at_jst:%m/%d %H:%M}）から{MISSED_HOURS}時間以上過ぎたため、投稿を見送りました。"
                print(f"見送り: {label} ({did})")
                write_result(did, "missed", error=msg)
                continue

            if uid is None:
                account = me()
                uid = account["user_id"]
                print(f"投稿先: @{account.get('username')}")

            if job.get("format") == "reel":
                link = post_reel(uid, did, job.get("caption", ""), job.get("video") or "reel.mp4", job_dir)
            else:
                link = post_carousel(uid, did, job.get("caption", ""), job.get("images") or [])

            posted_at = datetime.now(JST).isoformat(timespec="minutes")
            print(f"投稿完了: {label} ({did}) -> {link}")
            write_result(did, "posted", permalink=link, posted_at=posted_at)
        except Exception as e:
            print(f"投稿失敗: {label} ({did}): {e}")
            write_result(did, "failed", error=str(e))
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
