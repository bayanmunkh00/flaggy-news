#!/usr/bin/env python3
"""Build the static site (data/articles -> public/).

Pages: /  (latest), /c/<section>/ and /t/<subtopic>/ (30 per page), /a/<id>.html, search.json.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path

from newsbot import store
from newsbot.config import ROOT, Config, load_config

PER_PAGE = 30
HOME_COUNT = 60
SITE_DIR = ROOT / "site"


def e(text: str) -> str:
    return html.escape(text or "", quote=True)


def page(cfg: Config, title: str, body: str, depth: int, nav: str = "") -> str:
    up = "../" * depth
    return f"""<!doctype html>
<html lang="mn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="{up}assets/style.css">
<script>document.documentElement.dataset.theme = localStorage.getItem('theme') || '';</script>
</head>
<body data-root="{up}">
<header class="top">
  <a class="brand" href="{up}index.html">{e(cfg.site_title)}</a>
  <input id="q" type="search" placeholder="Хайх…" autocomplete="off">
  <button id="theme" aria-label="Theme">◐</button>
</header>
<nav class="cats">{nav}</nav>
<main>
<div id="results" hidden></div>
<div id="content">{body}</div>
</main>
<script src="{up}assets/app.js"></script>
</body>
</html>
"""


def sub_tag(cfg: Config, a: dict) -> str:
    sub = cfg.subtopics.get(a.get("subtopic", ""))
    return sub.hashtag if sub else a.get("section", "")


def card(a: dict, cfg: Config, up: str) -> str:
    cat = cfg.sections.get(a["section"])
    if a.get("card"):
        img = f'<img src="{up}a/{a["id"]}.png" alt="" loading="lazy">'
    else:
        img = f'<img src="{e(a["image"])}" alt="" loading="lazy" referrerpolicy="no-referrer">' if a.get("image") else ""
    summary = a.get("summary_mn") or a.get("summary_en") or (a.get("lines") or [""])[0]
    return f"""<article class="card">
  <a class="media" href="{up}a/{a['id']}.html">{img}</a>
  <div class="text">
    <div class="meta"><span class="tag">{cat.emoji if cat else ''} {e(cat.name if cat else a['section'])}</span>
      <span class="sub">#{e(sub_tag(cfg, a))}</span>
      <span>{e(a.get('source_label') or a['source'])}</span>
      <time datetime="{e(a['added'])}">{e(a['added'][:10])}</time></div>
    <h2><a href="{up}a/{a['id']}.html">{e(a.get('title_mn') or a['title'])}</a></h2>
    <p>{e(summary)}</p>
  </div>
</article>"""


def article_page(a: dict, cfg: Config, nav: str) -> str:
    cat = cfg.sections.get(a["section"])
    title = a.get("title_mn") or a["title"]
    img = f'<img class="hero" src="{e(a["image"])}" alt="" referrerpolicy="no-referrer">' if a.get("image") else ""
    if a.get("card"):
        img = f'<img class="hero" src="{a["id"]}.png" alt="">'
    if a.get("body_mn"):
        paras = [p for p in a["body_mn"].split("\n") if p.strip()]
    elif a.get("lines"):
        paras = [ln for ln in a["lines"] if ln.strip()]
    else:
        paras = [a.get("summary_en", "")]
    body = "\n".join(f"<p>{e(p)}</p>" for p in paras)
    original = f'<p class="orig">Original: {e(a["title"])}</p>' if a.get("title_mn") else ""
    inner = f"""<article class="full">
  <div class="meta"><a class="tag" href="../c/{a['section']}/index.html">{cat.emoji if cat else ''} {e(cat.name if cat else a['section'])}</a>
    <a class="tag" href="../t/{a.get('subtopic', '')}/index.html">#{e(sub_tag(cfg, a))}</a>
    <span>{e(a.get('source_label') or a['source'])}</span>
    <time datetime="{e(a['added'])}">{e(a['added'][:16].replace('T', ' '))} UTC</time></div>
  <h1>{e(title)}</h1>
  {img}
  {body}
  {original}
  <p><a class="btn" href="{e(a['url'])}" rel="noopener">Эх сурвалж →</a></p>
</article>"""
    return page(cfg, f"{title} · {cfg.site_title}", inner, 1, nav)


def build(data_dir: Path, out: Path) -> int:
    cfg = load_config()
    arts = sorted(store.load_articles(data_dir), key=lambda a: a["added"], reverse=True)
    if out.exists():
        shutil.rmtree(out)
    (out / "a").mkdir(parents=True)
    shutil.copytree(SITE_DIR / "assets", out / "assets")

    counts = {cid: sum(1 for a in arts if a["section"] == cid) for cid in cfg.sections}

    def nav(depth: int) -> str:
        up = "../" * depth
        links = [f'<a href="{up}index.html">Бүгд</a>']
        links += [f'<a href="{up}c/{c.id}/index.html">{c.emoji} {e(c.name)} <small>{counts[c.id]}</small></a>'
                  for c in cfg.sections.values() if counts[c.id]]
        return "".join(links)

    home = "\n".join(card(a, cfg, "") for a in arts[:HOME_COUNT]) or "<p class='empty'>Мэдээ удахгүй гарна.</p>"
    (out / "index.html").write_text(page(cfg, cfg.site_title, f'<div class="grid">{home}</div>', 0, nav(0)),
                                    encoding="utf-8")

    def listing(folder: str, key: str, title: str, items: list[dict], chips: str = "") -> None:
        pages = [items[i:i + PER_PAGE] for i in range(0, len(items), PER_PAGE)]
        for n, chunk in enumerate(pages):
            name = "index.html" if n == 0 else f"page-{n + 1}.html"
            pager = ""
            if len(pages) > 1:
                pager = '<div class="pager">' + " ".join(
                    f'<a href="{"index.html" if i == 0 else f"page-{i + 1}.html"}"'
                    f'{" class=on" if i == n else ""}>{i + 1}</a>' for i in range(len(pages))) + "</div>"
            body = f'<h1 class="cat">{e(title)}</h1>{chips}<div class="grid">' + \
                "\n".join(card(a, cfg, "../../") for a in chunk) + f"</div>{pager}"
            d = out / folder / key
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(page(cfg, f"{title} · {cfg.site_title}", body, 2, nav(2)), encoding="utf-8")

    for cid, sec in cfg.sections.items():
        items = [a for a in arts if a["section"] == cid]
        if not items:
            continue
        chips = '<nav class="cats">' + "".join(
            f'<a href="../../t/{s.id}/index.html">#{e(s.hashtag)} <small>'
            f'{sum(1 for a in items if a["subtopic"] == s.id)}</small></a>'
            for s in sec.subtopics if any(a["subtopic"] == s.id for a in items)) + "</nav>"
        listing("c", cid, f"{sec.emoji} {sec.name}", items, chips if len(sec.subtopics) > 1 else "")
    for sid, st in cfg.subtopics.items():
        items = [a for a in arts if a["subtopic"] == sid]
        if items:
            listing("t", sid, f"{st.section.emoji} {st.section.name} · #{st.hashtag}", items)

    for a in arts:
        if a.get("card") and (data_dir / a["card"]).exists():
            shutil.copyfile(data_dir / a["card"], out / "a" / f"{a['id']}.png")
        (out / "a" / f"{a['id']}.html").write_text(article_page(a, cfg, nav(1)), encoding="utf-8")

    index = [{"id": a["id"], "t": a.get("title_mn") or a["title"], "o": a["title"],
              "c": a["section"], "d": a["added"][:10],
              "s": (a.get("summary_mn") or a.get("summary_en") or "")[:200]} for a in arts]
    (out / "search.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    return len(arts)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=store.DATA_DIR)
    p.add_argument("--out", type=Path, default=ROOT / "public")
    args = p.parse_args()
    print(f"Built {build(args.data_dir, args.out)} articles -> {args.out}")


if __name__ == "__main__":
    main()
