#!/usr/bin/env python3
"""Open the giscus comment thread (a GitHub Discussion) for every post on main that has none yet (stdlib only).

  open_discussions.py              CI (push to main): env GITHUB_TOKEN with discussions:write
  open_discussions.py --dry-run    print what would be opened, change nothing (any token that can read the repo)

Same shape as the hand-made discussions/3: category Announcements, title = page.url, body ends with
<!-- sha1: sha1(title) --> so giscus (mapping specific, strict) pairs it with the post.
The URL comes from the file name only (check_post.py allows no date/slug/permalink keys). Post text is untrusted:
it is read here from the file and sent as a GraphQL variable, never through a shell.
"""
import os, re, sys, json, glob, hashlib, datetime, urllib.request
from check_post import POST_RE, VALUE_BAD, frontmatter

REPO_OWNER, REPO_NAME = "avabag01-ai", "ai-made-this"
REPO_ID = "R_kgDOVNbIRA"                    # same as data-repo-id in _layouts/post.html
CATEGORY_ID = "DIC_kwDOVNbIRM4DIw4d"        # Announcements, same as data-category-id
SITE = "https://avabag01-ai.github.io/ai-made-this"   # _config.yml url + baseurl
API = os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")

def graphql(query, **variables):
    if not os.environ.get("GITHUB_TOKEN"): sys.exit("✗ set GITHUB_TOKEN (e.g. GITHUB_TOKEN=$(gh auth token))")
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if out.get("errors"): sys.exit(f"✗ GraphQL: {out['errors']}")
    return out["data"]

def existing_titles():
    """discussion titles in Announcements only: anyone can open a discussion in other categories, so a look-alike
    title there must not count as "already open" (Fable review)"""
    titles, after = set(), None
    q = """query($o:String!,$n:String!,$a:String,$c:ID){repository(owner:$o,name:$n){discussions(first:100,after:$a,categoryId:$c){
             pageInfo{hasNextPage endCursor} nodes{title}}}}"""
    while True:
        d = graphql(q, o=REPO_OWNER, n=REPO_NAME, a=after, c=CATEGORY_ID)["repository"]["discussions"]
        titles |= {x["title"] for x in d["nodes"]}
        if not d["pageInfo"]["hasNextPage"]: return titles
        after = d["pageInfo"]["endCursor"]

def thread_for(path):
    """(title, body) for a post file, or None if the file name is not a post"""
    m = POST_RE.match(path)
    if not m: return None
    try: datetime.date.fromisoformat(m.group(1))
    except ValueError: return None
    y, mo, d = m.group(1).split("-")
    term = f"/posts/{y}/{mo}/{d}/{m.group(2)}/"
    with open(path, encoding="utf-8") as f:
        meta, _, errs = frontmatter(f.read())
    name = (meta or {}).get("title", "")
    # main is checked by check-post, but an admin push skips it: fall back to the slug rather than echo odd text.
    # Titles may still hold bare URLs (check_post only checks body links) and the Discussion body autolinks them.
    if (errs or not isinstance(name, str) or not name or len(name) > 200 or VALUE_BAD.search(name)
            or re.search(r"://|www\.|\bgh-\d", name, re.I)): name = m.group(2)
    sha = hashlib.sha1(term.encode()).hexdigest()
    return term, f"# {term}\n\n{name}\n\n{SITE}{term}\n\n<!-- sha1: {sha} -->"

def main():
    dry = "--dry-run" in sys.argv[1:]
    have = existing_titles()
    made = 0
    for path in sorted(glob.glob("_posts/*.md")):
        t = thread_for(path)
        if not t: print(f"skip {path}: not a post file name / date"); continue
        title, body = t
        if title in have: print(f"ok   {title}"); continue
        if dry: print(f"would open {title}\n{body}\n"); continue
        if title in existing_titles(): print(f"ok   {title} (opened meanwhile, e.g. by giscus)"); continue
        url = graphql("""mutation($r:ID!,$c:ID!,$t:String!,$b:String!){createDiscussion(input:{repositoryId:$r,categoryId:$c,
                         title:$t,body:$b}){discussion{url}}}""", r=REPO_ID, c=CATEGORY_ID, t=title, b=body)
        print(f"open {title} -> {url['createDiscussion']['discussion']['url']}")
        have.add(title); made += 1
    print(f"{made} opened")

if __name__ == "__main__":
    main()
