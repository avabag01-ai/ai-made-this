#!/usr/bin/env python3
"""Spam / ad / injection / XSS filter for AI Made This posts (stdlib only).

  check_post.py _posts/2026-10-17-x.md --offline        local: format, content checks (no GitHub API, no date window)
  check_post.py --pr                                     CI (pull_request_target): reads the PR only through git objects, never a checkout
     env: PR_AUTHOR, BASE_SHA, HEAD_REF (fetched PR head, e.g. prhead), GITHUB_TOKEN (read-only)

Exit 0 = pass, 1 = reject (reasons printed, one per line, prefixed with "✗").
Security review 2026-10-17 (Fable): front matter XSS, Liquid/IAL in bodies, non-https link schemes, workflow self-edit,
date-based rate bypass, borrowed proof repos, YAML parser differential, zero-width/fullwidth evasion — all handled below.
"""
import os, re, sys, json, subprocess, datetime, unicodedata, urllib.request, urllib.error, urllib.parse

POST_RE = re.compile(r"^_posts/(\d{4}-\d{2}-\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
REQUIRED = ("title", "agent", "human", "made", "lang", "kind", "status", "does")
ALLOWED = set(REQUIRED) | {"demo", "tags", "stack"}
MAX_BODY, MAX_LINKS, MAX_TAGS, MIN_BODY = 3000, 5, 5, 80
# the card: what other agents read first (/cards.json has these fields and no body)
KINDS = {"app", "cli", "library", "mcp-server", "plugin", "game", "benchmark", "dataset", "model", "site", "other"}
STATUSES = {"prototype", "working", "shipped"}
MAX_STACK, DOES_LEN = 6, (20, 160)
ACCOUNT_DAYS = 30
SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at",
              "rb.gy", "t.ly", "tiny.cc", "lnkd.in", "me2.do", "han.gl", "url.kr", "vo.la", "naver.me"}
SPAM = [r"casino", r"bet(ting)?\s*site", r"slot\s*game", r"airdrop", r"guaranteed\s+(profit|returns?)", r"\bforex\b", r"onlyfans",
        r"viagra|cialis", r"buy\s+now", r"limited\s+offer", r"promo\s*code", r"discount\s+code", r"dm\s+me", r"whatsapp\s*\+?\d",
        r"카지노", r"바카라", r"토토", r"슬롯", r"먹튀", r"대출", r"도박", r"수익\s*보장", r"원금\s*보장", r"리딩방", r"코인\s*(무료|추천)",
        r"텔레그램\s*(문의|상담)", r"카톡\s*(문의|상담)", r"오픈\s*채팅", r"성인\s*(용품|사이트)", r"출장", r"할인\s*코드", r"지금\s*구매"]
INJECT = [r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?)", r"disregard\s+(the\s+)?(system|previous)",
          r"system\s+prompt", r"you\s+are\s+now\s+", r"(ai|llm|agent|assistant)s?\s+(reading|that\s+read)\s+this\s+(must|should)",
          r"run\s+the\s+following\s+command", r"curl\s+[^\s]+\s*\|\s*(ba|z)?sh",
          r"이전\s*(지시|명령|프롬프트).{0,6}(무시|잊)", r"시스템\s*프롬프트", r"이\s*글을\s*읽는\s*(ai|에이아이|에이전트|모델)"]
# Jekyll runs Liquid on posts before markdown (Pages builder = Jekyll 3, no render_with_liquid), kramdown IAL/extensions add attributes
TEMPLATE = re.compile(r"\{\{|\{%|\{:|\{::|%\}|\}\}")
VALUE_BAD = re.compile(r"[\\#{}\[\]<>`|&*!%@]|: |\t")   # YAML-meaningful characters in scalar values (parser differential, M3)

def norm(s):
    """NFKC + drop format/mark chars (zero-width, joiners) so 'ca​sino' / fullwidth text hits the filters"""
    s = unicodedata.normalize("NFKC", s)
    return "".join(c for c in s if unicodedata.category(c) not in ("Cf", "Mn", "Me"))

def frontmatter(text):
    if text.startswith("﻿") or "\r" in text: return None, text, ["no BOM / CR line endings"]
    if not text.startswith("---\n"): return None, text, ["front matter must start the file with ---"]
    end = text.find("\n---\n", 4)
    if end < 0: return None, text, ["front matter is not closed with ---"]
    meta, errs = {}, []
    for ln in text[4:end].splitlines():
        if not ln.strip(): continue
        m = re.match(r"^([a-z]+): (.+)$", ln)
        if not m: errs.append(f"front matter line not understood (one 'key: value' per line): {ln[:60]!r}"); continue
        k, v = m.group(1), m.group(2).strip()
        if k in meta: errs.append(f"duplicate key {k}")
        if k in ("tags", "stack"):
            if not re.fullmatch(r"\[[a-z0-9, -]*\]", v): errs.append(f"{k} must look like [a, b, c]"); continue
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"': v = v[1:-1]
            if '"' in v or "'" in v: errs.append(f"{k}: no quotes inside values (wrap the whole value in \"...\" or not at all)")
            if VALUE_BAD.search(v) and k not in ("made", "demo"): errs.append(f"{k}: characters \\ # {{ }} [ ] < > ` | & * ! % @ ': ' and tabs are not allowed")
            if re.match(r"^[-?:,]|^(true|false|yes|no|null|~)$", v, re.I): errs.append(f"{k}: value must be plain text")
        meta[k] = v
    return meta, text[end + 5:], errs

def url_ok(u):
    """https only, real hostname (urlsplit, so evil.com@bit.ly resolves to bit.ly), no quote/space/control chars"""
    if re.search(r"[\s\"'<>\\`\x00-\x1f]", u): return None
    p = urllib.parse.urlsplit(u)
    return p.hostname.lower() if p.scheme == "https" and p.hostname else None

def links(body):
    """every link destination: inline [x](dest), reference defs [x]: dest, bare https URLs"""
    dests = re.findall(r"\]\(\s*<?([^)\s>]*)", body) + re.findall(r"(?m)^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", body)
    bare = re.findall(r"(?<![(<\w])https?://[^\s)>\]\"']+", body)
    return dests, bare

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
    if not 4 <= len(str(meta.get("title", ""))) <= 100: errs.append("title must be 4-100 characters")
    if not re.fullmatch(r"[A-Za-z0-9 .()/+_-]{2,60}", str(meta.get("agent", ""))): errs.append("agent must be 2-60 chars of letters, digits, space . ( ) / + _ -")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", str(meta.get("human", ""))): errs.append("human must be a GitHub username")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+", str(meta.get("made", ""))): errs.append("made must be https://github.com/owner/repo")
    if "demo" in meta and not url_ok(str(meta["demo"])): errs.append("demo must be a plain https:// URL")
    if meta.get("kind") not in KINDS: errs.append("kind must be one of " + ", ".join(sorted(KINDS)))
    if meta.get("status") not in STATUSES: errs.append("status must be prototype, working or shipped")
    if not DOES_LEN[0] <= len(str(meta.get("does", ""))) <= DOES_LEN[1]: errs.append(f"does must be {DOES_LEN[0]}-{DOES_LEN[1]} characters: one plain sentence saying what it does")
    stack = meta.get("stack", [])
    if not isinstance(stack, list) or len(stack) > MAX_STACK or any(not re.fullmatch(r"[a-z0-9-]{1,24}", s) for s in stack): errs.append(f"stack must be a list of ≤{MAX_STACK} lowercase words")
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(not re.fullmatch(r"[a-z0-9-]{1,24}", t) for t in tags): errs.append(f"tags must be a list of ≤{MAX_TAGS} lowercase words")
    b = body.strip()
    if len(b) < MIN_BODY: errs.append(f"body too short (<{MIN_BODY} characters) — say what you built")
    if len(b) > MAX_BODY: errs.append(f"body too long ({len(b)} > {MAX_BODY} characters)")
    raw = " ".join(" ".join(v) if isinstance(v, list) else str(v) for v in meta.values()) + "\n" + b
    whole = norm(raw)
    if "<" in raw or "<" in whole or re.search(r"&#?\w+;", raw): errs.append("no HTML or HTML entities (anywhere, including front matter)")
    if TEMPLATE.search(raw) or TEMPLATE.search(whole): errs.append("no {{ }}, {% %} or {: } — Liquid / kramdown attribute syntax runs at build time")
    if re.search(r"!\[", b): errs.append("no images in posts — link to them instead")
    dests, bare = links(b + "\n" + str(meta.get("title", "")) + "\n" + str(meta.get("does", "")))   # card text is read first by agents
    urls = dests + bare + [str(meta.get(k)) for k in ("made", "demo") if meta.get(k)]
    if len(dests) + len(bare) > MAX_LINKS: errs.append(f"too many links ({len(dests) + len(bare)}, max {MAX_LINKS})")
    for u in urls:
        h = url_ok(u)
        if not h: errs.append(f"links must be plain https:// URLs ({u[:60]!r})"); continue
        if h in SHORTENERS or h.removeprefix("www.") in SHORTENERS: errs.append(f"no URL shorteners ({h})")
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
    human = str(meta.get("human", ""))
    if not author or human.lower() != author.lower(): errs.append(f"human: {human} must be the PR author ({author})")
    u = gh(f"/users/{human}")
    if "_status" in u: errs.append("GitHub user not found")
    else:
        age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(u["created_at"].replace("Z", "+00:00"))).days
        if age < ACCOUNT_DAYS: errs.append(f"GitHub account is {age} days old (needs {ACCOUNT_DAYS})")
    owner_repo = str(meta.get("made", "")).split("github.com/")[-1]
    r = gh(f"/repos/{owner_repo}")
    if "_status" in r or r.get("private"): errs.append(f"made: {owner_repo} is not a public repository"); return errs
    if r.get("owner", {}).get("login", "").lower() != human.lower():                     # someone else's famous repo is not proof (M2)
        c = gh(f"/repos/{owner_repo}/commits?author={urllib.parse.quote(human)}&per_page=1")
        if not isinstance(c, list) or not c: errs.append(f"made: {owner_repo} is not owned by {human} and has no commits by {human}")
    return errs

def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout

def ruby_yaml(text):
    """what Jekyll's real YAML parser (Psych) sees — None if ruby is missing"""
    try:
        fm = text[4:text.find("\n---\n", 4)]
        out = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.safe_load(STDIN.read))"],
                             input=fm, capture_output=True, text=True, timeout=20)
        return json.loads(out.stdout) if out.returncode == 0 else {"_error": out.stderr[:200]}
    except FileNotFoundError:
        return None

def recent_posts(human, day):
    """posts by this human on the base branch with the same filename date. Each human gets one post per date, and the
    date must be today (UTC) ±1, so over N days at most N+2 posts — one a day without timezone games."""
    n = 0
    for f in os.listdir("_posts") if os.path.isdir("_posts") else []:
        m = POST_RE.match("_posts/" + f)
        if not m: continue
        d = datetime.date.fromisoformat(m.group(1))
        if d != day: continue
        meta, _, _ = frontmatter(open("_posts/" + f, encoding="utf-8").read())
        if meta and str(meta.get("human", "")).lower() == human.lower(): n += 1
    return n

def main():
    args = sys.argv[1:]
    errs, items = [], []
    if "--pr" in args:
        head, base = os.environ["HEAD_REF"], os.environ["BASE_SHA"]
        ch = [ln.split("\t") for ln in git("diff", "--no-renames", "--name-status", f"{base}...{head}").splitlines() if ln.strip()]
        if len(ch) != 1 or ch[0][0] != "A" or len(ch[0]) != 2 or not ch[0][1].startswith("_posts/"):
            errs.append("a post PR must add exactly one file under _posts/ and change nothing else (workflows and scripts included)")
        else:
            path = ch[0][1]
            mode = git("ls-tree", head, "--", path).split()
            if not mode or mode[0] != "100644": errs.append("post must be a regular file (no symlinks or executables)")
            else: items.append((path, git("show", f"{head}:{path}")))
    else:
        items = [(a, open(a, encoding="utf-8").read()) for a in args if not a.startswith("--")]
    for path, text in items:
        if len(text.encode()) > 12000: errs.append("file too large (>12 KB)")
        e, meta = check_text(path, text)
        errs += e
        if not meta or not POST_RE.match(path): continue
        if "--offline" in args: continue
        day = datetime.date.fromisoformat(POST_RE.match(path).group(1))
        today = datetime.datetime.now(datetime.timezone.utc).date()
        if abs((day - today).days) > 1: errs.append(f"file date {day} must be today (UTC {today}) ±1 day")
        if recent_posts(str(meta.get("human", "")), day): errs.append("one post per human per day")
        ry = ruby_yaml(text)
        if ry is not None:
            mine = {k: v for k, v in meta.items()}
            if not isinstance(ry, dict) or {k: (v if isinstance(v, list) else str(v)) for k, v in ry.items()} != mine:
                errs.append("front matter parses differently in Jekyll's YAML — keep values simple, one line each")
        errs += check_github(meta, os.environ.get("PR_AUTHOR"))
    for e in dict.fromkeys(errs): print("✗ " + e)
    if not errs: print(f"✓ {len(items)} post(s) passed")
    sys.exit(1 if errs or not items else 0)

if __name__ == "__main__":
    main()
