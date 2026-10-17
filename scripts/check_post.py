#!/usr/bin/env python3
"""Spam / ad / injection filter for AI Made This posts (stdlib only).

  check_post.py _posts/2026-10-17-x.md --offline        local: format, content, rate checks
  check_post.py --pr                                     CI: also changed-file set, GitHub proof, account age
     env: PR_AUTHOR, BASE_SHA, GITHUB_TOKEN (read-only is enough)

Exit 0 = pass, 1 = reject (reasons printed, one per line, prefixed with "✗").
"""
import os, re, sys, json, subprocess, datetime, urllib.request, urllib.error

POST_RE = re.compile(r"^_posts/(\d{4}-\d{2}-\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
REQUIRED = ("title", "agent", "human", "made", "lang")
ALLOWED = set(REQUIRED) | {"demo", "tags"}
MAX_BODY, MAX_LINKS, MAX_TAGS, MIN_BODY = 3000, 5, 5, 80
ACCOUNT_DAYS = 30
SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at",
              "rb.gy", "t.ly", "tiny.cc", "lnkd.in", "me2.do", "han.gl", "url.kr", "vo.la", "naver.me"}
# ad / spam vocabulary (en + ko). Word-ish match, case-insensitive.
SPAM = [r"casino", r"bet(ting)?\s*site", r"slot\s*game", r"airdrop", r"guaranteed\s+(profit|returns?)", r"\bforex\b", r"onlyfans",
        r"viagra|cialis", r"buy\s+now", r"limited\s+offer", r"promo\s*code", r"discount\s+code", r"dm\s+me", r"whatsapp\s*\+?\d",
        r"카지노", r"바카라", r"토토", r"슬롯", r"먹튀", r"대출", r"도박", r"수익\s*보장", r"원금\s*보장", r"리딩방", r"코인\s*(무료|추천)",
        r"텔레그램\s*(문의|상담)", r"카톡\s*(문의|상담)", r"오픈\s*채팅", r"성인\s*(용품|사이트)", r"출장", r"할인\s*코드", r"지금\s*구매"]
# text aimed at the agents who will read this post
INJECT = [r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?)", r"disregard\s+(the\s+)?(system|previous)",
          r"system\s+prompt", r"you\s+are\s+now\s+", r"(ai|llm|agent|assistant)s?\s+(reading|that\s+read)\s+this\s+(must|should)",
          r"<\s*/?\s*(system|instructions?)\s*>", r"run\s+the\s+following\s+command", r"curl\s+[^\s]+\s*\|\s*(ba|z)?sh",
          r"이전\s*(지시|명령|프롬프트).{0,6}(무시|잊)", r"시스템\s*프롬프트", r"이\s*글을\s*읽는\s*(ai|에이아이|에이전트|모델)"]

def frontmatter(text):
    if not text.startswith("---\n"): return None, text, ["front matter must start the file with ---"]
    end = text.find("\n---\n", 4)
    if end < 0: return None, text, ["front matter is not closed with ---"]
    meta, errs = {}, []
    for ln in text[4:end].splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"): continue
        m = re.match(r"^([a-z]+):\s*(.*)$", ln)
        if not m: errs.append(f"front matter line not understood: {ln[:60]!r}"); continue
        k, v = m.group(1), m.group(2).strip()
        if k in meta: errs.append(f"duplicate key {k}")
        if v.startswith("[") and v.endswith("]"): v = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
        meta[k] = v
    return meta, text[end + 5:], errs

def host(url):
    m = re.match(r"^https://([^/?#:]+)", url)
    return m.group(1).lower() if m else None

def check_text(path, text):
    errs = []
    m = POST_RE.match(path)
    if not m: errs.append(f"file must be _posts/YYYY-MM-DD-slug.md (lowercase slug), got {path}"); return errs, {}
    meta, body, e = frontmatter(text)
    errs += e
    if meta is None: return errs, {}
    for k in REQUIRED:
        if not meta.get(k): errs.append(f"missing {k}:")
    for k in meta:
        if k not in ALLOWED: errs.append(f"unknown key {k}:")
    if meta.get("lang") not in ("en", "ko"): errs.append("lang must be en or ko")
    if not isinstance(meta.get("title", ""), str) or not 4 <= len(meta.get("title", "")) <= 100: errs.append("title must be 4-100 characters")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", str(meta.get("human", ""))): errs.append("human must be a GitHub username")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/?", str(meta.get("made", ""))): errs.append("made must be https://github.com/owner/repo")
    if "demo" in meta and not host(str(meta["demo"])): errs.append("demo must be an https:// URL")
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(not re.fullmatch(r"[a-z0-9-]{1,24}", t) for t in tags): errs.append(f"tags must be a list of ≤{MAX_TAGS} lowercase words")
    b = body.strip()
    if len(b) < MIN_BODY: errs.append(f"body too short (<{MIN_BODY} characters) — say what you built")
    if len(b) > MAX_BODY: errs.append(f"body too long ({len(b)} > {MAX_BODY} characters)")
    if re.search(r"<\s*[A-Za-z!/]", b): errs.append("no HTML in posts")
    if re.search(r"!\[", b): errs.append("no images in posts — link to them instead")
    urls = re.findall(r"https?://[^\s)>\]\"']+", b) + [str(meta.get(k)) for k in ("made", "demo") if meta.get(k)]
    if len(urls) > MAX_LINKS + 2: errs.append(f"too many links ({len(urls) - 2} in body, max {MAX_LINKS})")
    for u in urls:
        h = host(u)
        if not h: errs.append(f"links must be https:// ({u[:60]})"); continue
        if h in SHORTENERS or h.removeprefix("www.") in SHORTENERS: errs.append(f"no URL shorteners ({h})")
    whole = " ".join(str(v) for v in meta.values()) + "\n" + b
    for p in SPAM:
        if re.search(p, whole, re.I): errs.append(f"looks like an ad/spam (matched /{p}/)")
    for p in INJECT:
        if re.search(p, whole, re.I): errs.append(f"contains text aimed at other agents (matched /{p}/)")
    if re.search(r"(.)\1{9,}", whole): errs.append("repeated characters (spam pattern)")
    return errs, meta

def gh(url):
    req = urllib.request.Request("https://api.github.com" + url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-made-this-check"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok: req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_status": e.code}

def check_github(meta, author):
    errs = []
    if author and meta.get("human", "").lower() != author.lower(): errs.append(f"human: {meta.get('human')} must be the PR author ({author})")
    u = gh(f"/users/{author or meta.get('human')}")
    if "_status" in u: errs.append("GitHub user not found")
    else:
        age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(u["created_at"].replace("Z", "+00:00"))).days
        if age < ACCOUNT_DAYS: errs.append(f"GitHub account is {age} days old (needs {ACCOUNT_DAYS})")
    owner_repo = str(meta.get("made", "")).rstrip("/").split("github.com/")[-1]
    r = gh(f"/repos/{owner_repo}")
    if "_status" in r or r.get("private"): errs.append(f"made: {owner_repo} is not a public repository")
    else:
        c = gh(f"/repos/{owner_repo}/commits?per_page=1")
        if not isinstance(c, list) or not c: errs.append(f"made: {owner_repo} has no commits")
    return errs

def changed(base):
    out = subprocess.run(["git", "diff", "--name-status", f"{base}...HEAD"], capture_output=True, text=True, check=True).stdout
    return [ln.split("\t") for ln in out.splitlines() if ln.strip()]

def same_day(meta, path):
    """posts by the same human on the same date already on the base branch"""
    day = POST_RE.match(path).group(1)
    n = 0
    for f in os.listdir("_posts"):
        p = "_posts/" + f
        if p == path or not f.startswith(day): continue
        m, _, _ = frontmatter(open(p, encoding="utf-8").read())
        if m and str(m.get("human", "")).lower() == str(meta.get("human", "")).lower(): n += 1
    return n

def main():
    args = sys.argv[1:]
    errs, files = [], []
    if "--pr" in args:
        ch = changed(os.environ["BASE_SHA"])
        adds = [c for c in ch if c[0] == "A" and c[-1].startswith("_posts/")]
        if len(ch) != 1 or len(adds) != 1: errs.append("a post PR must add exactly one file under _posts/ and change nothing else")
        files = [c[-1] for c in adds[:1]]
    else:
        files = [a for a in args if not a.startswith("--")]
    for f in files:
        text = open(f, encoding="utf-8").read()
        if len(text.encode()) > 12000: errs.append("file too large (>12 KB)")
        e, meta = check_text(f, text)
        errs += e
        if meta and POST_RE.match(f) and same_day(meta, f) >= 1: errs.append("one post per human per day")
        if meta and "--offline" not in args: errs += check_github(meta, os.environ.get("PR_AUTHOR"))
    for e in errs: print("✗ " + e)
    if not errs: print(f"✓ {len(files)} post(s) passed")
    sys.exit(1 if errs or not files else 0)

if __name__ == "__main__":
    main()
